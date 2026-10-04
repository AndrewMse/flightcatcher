"""Hopwatch benchmarks, against a fake Wizz backend and emulated AWS.

    python -m bench.run                 # everything (~15 min)
    python -m bench.run --quick         # smaller, for a smoke test
    python -m bench.run --scenario recovery

Scenarios:

* **politeness** - N worker processes with the shared rate limit and cache
  (the real design) against N processes each with their own (the naive one):
  calls to Wizz, peak request rate, sweep time.
* **scaling** - a sweep whose timetables are all cached, so only CPU work is
  left: does adding workers help when the rate limit is not the bottleneck?
* **recovery** - SIGKILL random workers mid-job, over and over; then check
  nothing was lost, nothing ran twice, and the results match a clean run.
* **aws** - the same pipeline on SQS and DynamoDB (moto), counting every API
  call; feeds the monthly cost estimate.

Results go to bench/results/<timestamp>.json and bench/results/latest.md.
The real Wizz Air site is never contacted.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hopwatch import config
from hopwatch.jobs.sqlite_queue import SqliteJobQueue
from hopwatch.store import (
    JOB_DEAD,
    JOB_DONE,
    JOB_RUNNING,
    RUN_INCOMPLETE,
    RUN_OK,
    SqliteStore,
    Store,
)

from .cost import UsageCounts, monthly_cost
from .harness import (
    PREFIX,
    REGION,
    Workers,
    aws_env,
    enqueue,
    finished,
    local_env,
    percentile,
    pick_pairs,
    seed_wants,
    start_fake,
    start_moto,
    wait_for,
)

UTC = timezone.utc
RESULTS = Path(__file__).resolve().parent / "results"
REAL_INTERVAL_S = config.MIN_REQUEST_INTERVAL  # 1.5 s: the production politeness limit
SWEEPS_PER_MONTH = 30 * 24 * 60 // 15


def first_day(offset_weeks: int = 0) -> date:
    return date.today() + timedelta(days=3 + 7 * offset_weeks)


def wait_ready(store: Store, pool: Workers, since: datetime, timeout: float = 60) -> None:
    """Block until every worker in the pool has reported in."""
    prefixes = [pool.worker_prefix(p) for p in pool.procs]

    def ready() -> bool:
        beats = store.heartbeats()
        return all(
            any(name.startswith(f"worker:{prefix}") and ts >= since for name, ts in beats.items())
            for prefix in prefixes
        )

    if not wait_for(ready, timeout):
        raise RuntimeError("workers did not start")


def sweep_wall_seconds(store: Store, keys: list[str], started: float) -> float:
    ends = [store.get_job(k).finished_at for k in keys]
    return max(e.timestamp() for e in ends if e) - started


# --- politeness -------------------------------------------------------------


def scenario_politeness(quick: bool) -> dict[str, Any]:
    interval = 0.2
    counts = [1, 2, 4] if quick else [1, 2, 4, 8]
    fake = start_fake(stations=60, latency_ms=80)
    pairs = pick_pairs(fake.network(), 6 if quick else 12)
    rows = []
    try:
        for mode in ("shared", "naive"):
            for n in counts:
                with tempfile.TemporaryDirectory() as tmp:
                    workdir = Path(tmp)
                    store = SqliteStore(workdir / "bench.db")
                    queue = SqliteJobQueue(workdir / "bench.db")
                    want_ids = seed_wants(store, pairs, first_day(), 6)
                    pool = Workers(local_env(workdir, fake), workdir, mode=mode, interval=interval)
                    since = datetime.now(UTC)
                    pool.spawn(n)
                    wait_ready(store, pool, since)

                    fake.reset()
                    started = time.time()
                    keys = enqueue(store, queue, want_ids, "sweep")
                    if not wait_for(lambda: finished(store, keys), timeout=900):
                        raise RuntimeError(f"politeness {mode}/{n} did not finish")
                    wall = sweep_wall_seconds(store, keys, started)
                    stats = fake.stats()
                    pool.stop()
                    rows.append({
                        "mode": mode,
                        "workers": n,
                        "wall_s": round(wall, 2),
                        "wizz_calls": stats["total"],
                        "peak_requests_per_s": stats["max_per_second"],
                        "mean_requests_per_s": round(stats["total"] / wall, 2),
                    })
                    print(f"  politeness {mode:6s} x{n}: {rows[-1]}", flush=True)
                    queue.close()
                    store.close()
    finally:
        fake.stop()
    return {
        "params": {"wants": len(pairs), "interval_s": interval, "limit_per_s": 1 / interval,
                   "fake_latency_ms": 80, "days_per_want": 7},
        "rows": rows,
    }


# --- scaling ----------------------------------------------------------------


def scenario_scaling(quick: bool) -> dict[str, Any]:
    counts = [1, 2, 4] if quick else [1, 2, 4, 8]
    repeats = 2 if quick else 5
    fake = start_fake(stations=60, latency_ms=80)
    pairs = pick_pairs(fake.network(), 12 if quick else 40)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        store = SqliteStore(workdir / "bench.db")
        queue = SqliteJobQueue(workdir / "bench.db")
        want_ids = seed_wants(store, pairs, first_day(), 13)
        try:
            # Warm the shared cache once; every measured sweep after this is
            # pure CPU and database work.
            pool = Workers(local_env(workdir, fake), workdir, interval=0.05)
            pool.spawn(4)
            keys = enqueue(store, queue, want_ids, "warm")
            wait_for(lambda: finished(store, keys), timeout=900)
            pool.stop()

            for n in counts:
                pool = Workers(local_env(workdir, fake), workdir, interval=0.05)
                since = datetime.now(UTC)
                pool.spawn(n)
                wait_ready(store, pool, since)
                fake.reset()
                started = time.time()
                keys = [k for r in range(repeats) for k in enqueue(store, queue, want_ids, f"x{n}r{r}")]
                wait_for(lambda: finished(store, keys), timeout=900)
                wall = sweep_wall_seconds(store, keys, started)
                calls = fake.stats()["total"]
                pool.stop()
                rows.append({
                    "workers": n,
                    "wall_s": round(wall, 2),
                    "jobs_per_s": round(len(keys) / wall, 2),
                    "wizz_calls": calls,
                })
                print(f"  scaling x{n}: {rows[-1]}", flush=True)
        finally:
            fake.stop()
            queue.close()
            store.close()
    base = rows[0]["wall_s"]
    for row in rows:
        row["speedup"] = round(base / row["wall_s"], 2)
    return {"params": {"wants": len(pairs), "jobs": len(pairs) * repeats, "days_per_want": 14},
            "rows": rows}


# --- recovery ---------------------------------------------------------------


def _completed_runs_per_job(store: Store) -> Counter[str]:
    return Counter(
        run.job_key for run in store.list_search_runs(limit=1_000_000)
        if run.status in (RUN_OK, RUN_INCOMPLETE)
    )


def _candidate_set(store: Store) -> set[tuple[int, str]]:
    return {(c.want_id, c.signature) for c in store.list_candidates(limit=1_000_000)}


def chaos(store: Store, queue: Any, pool: Workers, pairs: list[tuple[str, str]], *,
          target_kills: int, max_rounds: int, kill_every_s: float, seed: int = 1) -> dict[str, Any]:
    """Run rounds of sweeps while killing a random worker every ``kill_every_s``."""
    rng = random.Random(seed)
    keys: list[str] = []
    kills: list[dict[str, Any]] = []
    rounds = 0

    def next_round() -> None:
        nonlocal rounds
        ids = seed_wants(store, pairs, first_day(rounds), 6)
        keys.extend(enqueue(store, queue, ids, f"r{rounds}"))
        rounds += 1

    next_round()
    while True:
        time.sleep(kill_every_s)
        if finished(store, keys):
            if len(kills) >= target_kills or rounds >= max_rounds:
                break
            next_round()
            continue
        victim = rng.choice(pool.procs)
        prefix = pool.worker_prefix(victim)
        held = [j.key for j in store.list_jobs(status=JOB_RUNNING, limit=1000)
                if (j.worker or "").startswith(prefix)]
        killed_at = datetime.now(UTC)
        pool.kill(victim)
        kills.append({"at": killed_at, "held": held})
        pool.spawn(1)

    recovery = []
    for kill in kills:
        for key in kill["held"]:
            job = store.get_job(key)
            if job and job.finished_at:
                recovery.append((job.finished_at - kill["at"]).total_seconds())

    statuses = Counter(store.get_job(k).status for k in keys)
    completions = _completed_runs_per_job(store)
    return {
        "rounds": rounds,
        "jobs": len(keys),
        "kills": len(kills),
        "kills_mid_job": sum(bool(k["held"]) for k in kills),
        "jobs_done": statuses.get(JOB_DONE, 0),
        "jobs_dead_lettered": statuses.get(JOB_DEAD, 0),
        "jobs_lost": len(keys) - statuses.get(JOB_DONE, 0) - statuses.get(JOB_DEAD, 0),
        "jobs_completed_twice": sum(1 for k in keys if completions.get(k, 0) > 1),
        "recovery_s": {
            "p50": percentile(recovery, 50),
            "p95": percentile(recovery, 95),
            "max": max(recovery) if recovery else None,
        },
    }


def scenario_recovery(quick: bool) -> dict[str, Any]:
    lease = 4
    fake = start_fake(stations=60, latency_ms=60)
    pairs = pick_pairs(fake.network(), 12)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            store = SqliteStore(workdir / "bench.db")
            queue = SqliteJobQueue(workdir / "bench.db", visibility_s=lease)
            pool = Workers(local_env(workdir, fake, visibility_s=lease), workdir, interval=0.05)
            since = datetime.now(UTC)
            pool.spawn(4)
            wait_ready(store, pool, since)
            result = chaos(store, queue, pool, pairs, target_kills=15 if quick else 50,
                           max_rounds=4 if quick else 14, kill_every_s=0.6)
            if not wait_for(lambda: finished(store, [j.key for j in store.list_jobs(limit=10_000)]),
                            timeout=300):
                print("  recovery: some jobs never finished", flush=True)
            pool.stop()
            chaos_candidates = _candidate_set(store)
            rounds = result["rounds"]
            queue.close()
            store.close()

        # The same work with nobody killed, as ground truth.
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            store = SqliteStore(workdir / "bench.db")
            queue = SqliteJobQueue(workdir / "bench.db")
            pool = Workers(local_env(workdir, fake), workdir, interval=0.05)
            pool.spawn(2)
            keys: list[str] = []
            for r in range(rounds):
                ids = seed_wants(store, pairs, first_day(r), 6)
                keys.extend(enqueue(store, queue, ids, f"r{r}"))
            wait_for(lambda: finished(store, keys), timeout=900)
            pool.stop()
            reference = _candidate_set(store)
            queue.close()
            store.close()
    finally:
        fake.stop()

    result["candidates"] = len(reference)
    result["candidates_missing"] = len(reference - chaos_candidates)
    result["candidates_extra"] = len(chaos_candidates - reference)
    result["params"] = {"workers": 4, "lease_s": lease, "kill_every_s": 0.6, "wants_per_round": 12}
    print(f"  recovery: {result}", flush=True)
    return result


# --- aws (moto) -------------------------------------------------------------


def _sum_counts(workdir: Path) -> dict[str, Any]:
    calls: Counter[str] = Counter()
    reads = writes = 0.0
    for path in workdir.glob("counts-*.json"):
        data = json.loads(path.read_text())
        calls.update(data["calls"])
        reads += data["dynamo_read_units"]
        writes += data["dynamo_write_units"]
    return {"calls": dict(calls), "dynamo_read_units": reads, "dynamo_write_units": writes}


def scenario_aws(quick: bool) -> dict[str, Any]:
    from hopwatch.aws.dynamo_store import DynamoStore
    from hopwatch.aws.sqs_queue import SqsJobQueue

    wants = 10
    fake = start_fake(stations=60, latency_ms=60)
    pairs = pick_pairs(fake.network(), wants)
    aws, stop_moto = start_moto(visibility_s=6)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            store = DynamoStore(PREFIX, REGION, aws["endpoint"])
            queue = SqsJobQueue(aws["queue_url"], REGION, aws["endpoint"], dlq_url=aws["dlq_url"])
            env = aws_env(workdir, fake, aws, visibility_s=6)

            # 1. One clean sweep, counting every AWS call the workers make.
            pool = Workers(env, workdir, interval=0.05, counts=True)
            since = datetime.now(UTC)
            pool.spawn(2)
            wait_ready(store, pool, since)
            want_ids = seed_wants(store, pairs, first_day(), 6)
            fake.reset()
            started = time.time()
            keys = enqueue(store, queue, want_ids, "count")
            if not wait_for(lambda: finished(store, keys), timeout=900, poll=0.5):
                raise RuntimeError("aws sweep did not finish")
            wall = sweep_wall_seconds(store, keys, started)
            wizz_calls = fake.stats()["total"]
            pool.stop()
            counted = _sum_counts(workdir)
            runs = [r for r in store.list_search_runs(limit=1000) if r.job_key in keys]
            cpu_s = sum((r.duration_ms or 0) for r in runs) / 1000
            log_bytes = pool.log_bytes()

            # 2. Chaos on the AWS backend: same guarantees as locally?
            pool = Workers(env, workdir, interval=0.05)
            since = datetime.now(UTC)
            pool.spawn(3)
            wait_ready(store, pool, since)
            chaos_result = chaos(store, queue, pool, pairs, target_kills=8 if quick else 20,
                                 max_rounds=3 if quick else 6, kill_every_s=1.0, seed=2)
            pool.stop()
    finally:
        stop_moto()
        fake.stop()

    calls = counted["calls"]
    jobs = len(keys)
    # Per sweep on AWS. The planner's share is added by hand: it is one
    # PutItem and one SendMessage per job, plus a few small reads.
    sqs = jobs + calls.get("sqs:DeleteMessage", 0) + calls.get("sqs:ChangeMessageVisibility", 0)
    dynamo_writes = counted["dynamo_write_units"] + jobs
    dynamo_reads = counted["dynamo_read_units"] + 4
    projections = {}
    for concurrency in (1, 2):
        # On Lambda a worker waiting on the shared 1.5 s limit is billed for the
        # wait, so duration follows the politeness limit, not the CPU work.
        worker_seconds = wizz_calls * REAL_INTERVAL_S * concurrency + cpu_s
        per_sweep = UsageCounts(
            jobs=jobs, sqs_requests=int(sqs), dynamo_reads=dynamo_reads,
            dynamo_writes=dynamo_writes, worker_seconds=worker_seconds,
            # Worker log lines, plus Lambda's own START/END/REPORT per invocation.
            log_bytes=log_bytes + 400 * (jobs + 1),
        )
        projections[f"concurrency_{concurrency}"] = {
            "per_sweep": per_sweep.__dict__,
            "list_price": monthly_cost(per_sweep, SWEEPS_PER_MONTH, free_tier=False).to_dict(),
            "with_free_tier": monthly_cost(per_sweep, SWEEPS_PER_MONTH, free_tier=True).to_dict(),
        }
    result = {
        "params": {"wants": wants, "sweeps_per_month": SWEEPS_PER_MONTH,
                   "projected_interval_s": REAL_INTERVAL_S},
        "sweep": {"jobs": jobs, "wall_s_emulated": round(wall, 2), "wizz_calls": wizz_calls,
                  "worker_cpu_s": round(cpu_s, 2), "aws_calls": calls,
                  "dynamo_read_units": dynamo_reads, "dynamo_write_units": dynamo_writes},
        "chaos": chaos_result,
        "monthly_cost": projections,
    }
    print(f"  aws: sweep {result['sweep']}", flush=True)
    print(f"  aws: chaos {chaos_result}", flush=True)
    return result


# --- report -----------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def markdown(results: dict[str, Any]) -> str:
    out = [f"# Benchmark results ({results['started'][:10]})", ""]
    if "politeness" in results:
        p = results["politeness"]
        prm = p["params"]
        out += [
            "## Politeness under scale-out",
            "",
            f"{prm['wants']} wants, cold cache, rate limit {prm['interval_s']}s "
            f"({prm['limit_per_s']:.0f} req/s), fake latency {prm['fake_latency_ms']} ms.",
            "",
            "| design | workers | sweep (s) | calls to Wizz | peak req/s | mean req/s |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in p["rows"]:
            out.append(f"| {row['mode']} | {row['workers']} | {_fmt(row['wall_s'])} | "
                       f"{row['wizz_calls']} | {row['peak_requests_per_s']} | "
                       f"{_fmt(row['mean_requests_per_s'])} |")
        out.append("")
    if "scaling" in results:
        s = results["scaling"]
        out += [
            "## Pipeline throughput with a warm cache",
            "",
            f"{s['params']['jobs']} jobs over {s['params']['wants']} wants, every timetable "
            "already cached: only queue, CPU and database work is left.",
            "",
            "| workers | sweep (s) | jobs/s | speed-up | calls to Wizz |",
            "|---:|---:|---:|---:|---:|",
        ]
        for row in s["rows"]:
            out.append(f"| {row['workers']} | {_fmt(row['wall_s'])} | {_fmt(row['jobs_per_s'])} | "
                       f"{_fmt(row['speedup'])}× | {row['wizz_calls']} |")
        out.append("")
    if "recovery" in results:
        r = results["recovery"]
        out += [
            "## Recovery from worker crashes (SQLite backend)",
            "",
            f"{r['params']['workers']} workers, a random one SIGKILLed every "
            f"{r['params']['kill_every_s']}s, lease {r['params']['lease_s']}s.",
            "",
            "| kills | mid-job | jobs | lost | dead-lettered | completed twice | "
            "candidates vs clean run | recovery p50 / p95 / max (s) |",
            "|---:|---:|---:|---:|---:|---:|---|---|",
            f"| {r['kills']} | {r['kills_mid_job']} | {r['jobs']} | {r['jobs_lost']} | "
            f"{r['jobs_dead_lettered']} | {r['jobs_completed_twice']} | "
            f"{r['candidates']} identical ({r['candidates_missing']} missing, "
            f"{r['candidates_extra']} extra) | {_fmt(r['recovery_s']['p50'])} / "
            f"{_fmt(r['recovery_s']['p95'])} / {_fmt(r['recovery_s']['max'])} |",
            "",
        ]
    if "aws" in results:
        a = results["aws"]
        c = a["chaos"]
        out += [
            "## AWS backend (SQS + DynamoDB on moto)",
            "",
            f"One cold sweep of {a['params']['wants']} wants: {a['sweep']['wizz_calls']} calls to "
            f"Wizz, {_fmt(a['sweep']['dynamo_write_units'])} DynamoDB write units, "
            f"{_fmt(a['sweep']['dynamo_read_units'])} read units.",
            "",
            f"Chaos: {c['kills']} kills ({c['kills_mid_job']} mid-job) over {c['jobs']} jobs — "
            f"{c['jobs_lost']} lost, {c['jobs_completed_twice']} completed twice, "
            f"{c['jobs_dead_lettered']} dead-lettered.",
            "",
            "Estimated monthly cost, eu-central-1, sweeping every 15 minutes:",
            "",
            "| worker concurrency | Lambda GB-s | list price | with always-free tier |",
            "|---:|---:|---:|---:|",
        ]
        for name, proj in a["monthly_cost"].items():
            out.append(f"| {name.split('_')[1]} | "
                       f"{proj['list_price']['usage']['lambda_gb_seconds']:,.0f} | "
                       f"${proj['list_price']['total']:.2f} | ${proj['with_free_tier']['total']:.2f} |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hopwatch benchmarks (fake Wizz, emulated AWS)")
    parser.add_argument("--scenario", default="all",
                        choices=["all", "politeness", "scaling", "recovery", "aws"])
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--out", default=str(RESULTS))
    args = parser.parse_args()

    scenarios = {
        "politeness": scenario_politeness,
        "scaling": scenario_scaling,
        "recovery": scenario_recovery,
        "aws": scenario_aws,
    }
    chosen = list(scenarios) if args.scenario == "all" else [args.scenario]
    results: dict[str, Any] = {"started": datetime.now(UTC).isoformat(), "quick": args.quick}
    for name in chosen:
        print(f"== {name}", flush=True)
        began = time.monotonic()
        results[name] = scenarios[name](args.quick)
        results[name]["took_s"] = round(time.monotonic() - began, 1)
    results["finished"] = datetime.now(UTC).isoformat()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (out / f"{stamp}.json").write_text(json.dumps(results, indent=2, default=str))
    (out / "latest.md").write_text(markdown(results))
    print(markdown(results))


if __name__ == "__main__":
    main()
