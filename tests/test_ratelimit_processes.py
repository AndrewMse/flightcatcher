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
    limiter.wait()
    print(time.time(), flush=True)
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
    stamps: list[float] = []
    for proc in procs:
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0
        stamps.extend(float(line) for line in out.split())

    stamps.sort()
    assert len(stamps) == 12
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= interval * 0.8
    # And it really is a shared budget, not three independent ones.
    assert stamps[-1] - stamps[0] >= interval * 11 * 0.8
