"""The project is called Hopwatch, everywhere a user can see it."""

from __future__ import annotations

import hopwatch
from hopwatch.cli import build_parser


def test_package_version() -> None:
    assert hopwatch.__version__ == "0.3.0"


def test_cli_is_named_hopwatch() -> None:
    assert build_parser().prog == "hopwatch"


def test_worker_command_is_wired() -> None:
    args = build_parser().parse_args(["worker", "--concurrency", "3"])
    assert args.concurrency == 3
    assert args.func.__name__ == "cmd_worker"
