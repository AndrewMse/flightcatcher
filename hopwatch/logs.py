"""Log formatting: plain text for a terminal, JSON lines for everything else.

In JSON mode every line is one object, and whatever ``log_context`` has in
scope -- the job, its attempt, the worker, the want, the booking -- rides
along on every line logged inside it. "Why did this sweep fail" then becomes
one filter on ``job`` rather than reading interleaved output from five workers.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Iterator, TextIO

TEXT_FORMAT = "%(levelname)s %(name)s: %(message)s"

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "hopwatch_log_context", default={}
)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach ``fields`` to every log line written inside this block."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    """Stamp the context onto the record when it is logged, not when formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.hopwatch_context = _context.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "hopwatch_context", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure(fmt: str = "text", level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Replace the root logger's handlers with one in the chosen format."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(_ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
