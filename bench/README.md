# Benchmarks

```bash
.venv/bin/python -m bench.run            # all scenarios, ~15 minutes
.venv/bin/python -m bench.run --quick    # smaller, ~3 minutes
.venv/bin/python -m bench.run --scenario recovery
```

Everything runs on localhost. Wizz Air is replaced by `fakewizz.py`, a seeded
synthetic network served over real HTTP, because measuring the real site would
mean hammering it, which the politeness limits exist to prevent. AWS is
replaced by moto, so no account or credentials are needed.

| scenario | question |
|---|---|
| `politeness` | Do more workers mean more traffic to Wizz? Shared rate limit and cache (the real design) vs per-process ones (the naive one). |
| `scaling` | When every timetable is cached, so only CPU and database work is left, do more workers help? |
| `recovery` | SIGKILL random workers mid-job, repeatedly. Is anything lost, run twice, or different from a clean run? |
| `aws` | The same pipeline on SQS and DynamoDB. What does a sweep cost in API calls, and per month? |

Each run writes `results/<timestamp>.json` with every parameter and number,
and `results/latest.md` with the tables the main README quotes.

Two things to keep in mind when reading the numbers:

- **Time is scaled.** The production limit is one call every 1.5 s. The
  benchmarks use 0.2 s (politeness) or 0.05 s (others) so a run takes minutes,
  not hours. Request counts and rates relative to the limit carry over; wall
  times do not.
- **moto is not AWS.** The `aws` scenario checks behaviour and counts requests.
  Its timings say nothing about real SQS or DynamoDB latency, and the monthly
  cost is an estimate from those counts and list prices (see `cost.py`).
