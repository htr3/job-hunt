"""Daily scheduler for the Job Hunt Agent.

Usage:
    python scheduler.py                # run forever, fire daily at config.scheduler.run_time
    python scheduler.py --now          # run once immediately, then exit
    python scheduler.py --at 09:15     # override the daily time
    python scheduler.py --config PATH  # non-default config

Notes:
- Uses the `schedule` library (pure-Python, no OS cron needed).
- Times are interpreted in the local timezone the process runs in.
- Ctrl+C (SIGINT) is honored and shuts down gracefully.
"""
from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
import threading
import time

try:
    import schedule  # type: ignore
except ImportError:  # pragma: no cover
    schedule = None  # type: ignore

from job_hunter import (
    DEFAULT_CONFIG_PATH,
    load_config,
    run_agent,
    setup_logging,
)

logger = logging.getLogger("scheduler")

_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")


def _valid_time(s: str) -> bool:
    return bool(_TIME_RE.match(s or ""))


def _run_once(config_path: str, no_apply: bool, platforms: list[str] | None) -> None:
    logger.info("Scheduled run starting...")
    try:
        config = load_config(config_path)
    except Exception as e:
        logger.error("Scheduled run: could not load config: %s", e)
        return
    try:
        summary = run_agent(
            config,
            platforms=platforms,
            new_only=False,
            no_apply=no_apply,
        )
        logger.info(
            "Scheduled run complete: scraped=%d new=%d duration=%.1fs",
            summary.get("total_scraped", 0),
            summary.get("new_jobs", 0),
            summary.get("duration_seconds", 0.0),
        )
    except Exception as e:
        logger.exception("Scheduled run failed: %s", e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job Hunt Agent daily scheduler")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument(
        "--at",
        default=None,
        help="Override daily run time (HH:MM, 24h local time). "
        "Defaults to config.scheduler.run_time.",
    )
    parser.add_argument(
        "--now", action="store_true", help="Run once immediately and exit."
    )
    parser.add_argument(
        "--no-apply", action="store_true", help="Pass --no-apply to each scheduled run."
    )
    parser.add_argument(
        "--platforms",
        nargs="+",
        metavar="NAME",
        help="Override enabled platforms for every scheduled run.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)

    if args.now:
        _run_once(args.config, args.no_apply, args.platforms)
        return 0

    if schedule is None:
        logger.error(
            "The `schedule` library is not installed. "
            "Run: pip install schedule   (or pip install -r requirements.txt)"
        )
        return 2

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 2

    run_time = args.at or (config.get("scheduler", {}) or {}).get("run_time") or "08:00"
    if not _valid_time(run_time):
        logger.error("Invalid run time %r (expected HH:MM).", run_time)
        return 2

    stop_event = threading.Event()

    def _sigint(_signum, _frame):  # noqa: ARG001
        logger.info("SIGINT received — scheduler shutting down.")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _sigint)
    except (ValueError, OSError):
        pass

    schedule.every().day.at(run_time).do(
        _run_once, args.config, args.no_apply, args.platforms
    )
    logger.info("Scheduler armed — will fire every day at %s local time.", run_time)
    logger.info("Press Ctrl+C to stop.")

    while not stop_event.is_set():
        schedule.run_pending()
        stop_event.wait(timeout=30)

    logger.info("Scheduler stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
