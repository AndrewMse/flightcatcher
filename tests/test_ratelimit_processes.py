"""The SQLite limiter spaces calls made by separate worker processes."""

from __future__ import annotations

import subprocess
import sys

CHILD = """
import sys, time
from pathlib import Path
from hopwatch.ratelimit import SqliteRateLimiter
limiter = SqliteRateLimiter(Path(sys.argv[1]), float(sys.argv[2]))
for _ in range(4):
    slot = limiter.wait()
    print(slot, time.time(), flush=True)
"""


def test_spacing_holds_across_processes(tmp_path) -> None:
    interval = 0.1
    db = tmp_path / "limit.db"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(db), str(interval)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    slots: list[float] = []
    stamps: list[float] = []
    for proc in procs:
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0
        for line in out.splitlines():
            slot, stamp = line.split()
            slots.append(float(slot))
            stamps.append(float(stamp))

    slots.sort()
    stamps.sort()
    assert len(slots) == 12
    # The slots granted across processes are exactly spaced...
    assert min(b - a for a, b in zip(slots, slots[1:])) >= interval * 0.99
    # ...and it really is a shared budget, not three independent ones.
    assert stamps[-1] - stamps[0] >= interval * 11 * 0.9
