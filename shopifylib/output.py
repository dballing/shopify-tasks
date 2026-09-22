#!/usr/bin/env python3
"""Logging, progress, and result emission.

Two rules shape this module:

**stdout is for results, stderr is for narration.** A task's machine-readable
output (``--json``) goes to stdout and nothing else does, so
``./shopify.sh export-products --json | jq ...`` works without filtering out
progress chatter. Human-facing logging goes to stderr, where it stays visible
even when stdout is piped.

**stdout is line-buffered.** When a long run is redirected to a log file, Python
block-buffers stdout by default, so ``tail -f`` shows nothing for minutes and
then a wall of text. Reconfiguring to line buffering is what makes a two-hour
bulk edit observable while it runs -- the same reason ingest.py and
rescore_viability.py do it in the jobsearch repo.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Iterable, Iterator

# Plain, greppable format. Timestamps are local and second-resolution: these
# logs get read next to a Shopify admin activity feed, not correlated across
# distributed services.
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """Configure root logging to stderr and line-buffer stdout."""
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # urllib3 logs every connection at DEBUG, which buries our own output under
    # -v. Keep it one level quieter than whatever we are running at.
    logging.getLogger("urllib3").setLevel(max(level, logging.INFO))

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        # Not a real stream (pytest capture, a pipe wrapper). Nothing to do.
        pass

    return logging.getLogger("shopify-tasks")


def emit_json(value: Any) -> None:
    """Write a task's machine-readable result to stdout.

    ``default=str`` keeps a stray date or Decimal from turning a completed
    two-hour export into a TypeError at the moment it tries to print.
    """
    json.dump(value, sys.stdout, indent=2, default=str, ensure_ascii=False)
    sys.stdout.write("\n")


def emit_jsonl(rows: Iterable[Any]) -> int:
    """Stream rows to stdout as JSON Lines; returns the count written.

    Preferred over ``emit_json`` for exports: it streams instead of building the
    whole list in memory, and the output stays usable if the run is interrupted.
    """
    count = 0
    for row in rows:
        json.dump(row, sys.stdout, default=str, ensure_ascii=False)
        sys.stdout.write("\n")
        count += 1
    return count


class Progress:
    """Periodic progress reporting for a long loop, written to stderr.

    Reports on an interval rather than every N items because the useful cadence
    depends on throughput, which varies with query cost and rate-limit pacing.
    """

    def __init__(self, label: str, *, total: int | None = None, interval: float = 5.0):
        self.label = label
        self.total = total
        self.interval = interval
        self.count = 0
        self._log = logging.getLogger("shopify-tasks.progress")
        self._started = time.monotonic()
        self._last_report = self._started

    def tick(self, n: int = 1) -> None:
        self.count += n
        now = time.monotonic()
        if now - self._last_report >= self.interval:
            self._last_report = now
            self._log.info("%s", self._render(now))

    def done(self) -> None:
        self._log.info("%s (done)", self._render(time.monotonic()))

    def wrap(self, items: Iterable) -> Iterator:
        """Drive the counter from an iterable: ``for x in progress.wrap(it):``"""
        for item in items:
            yield item
            self.tick()
        self.done()

    def _render(self, now: float) -> str:
        elapsed = max(now - self._started, 1e-9)
        rate = self.count / elapsed
        if self.total:
            pct = 100.0 * self.count / self.total
            remaining = (self.total - self.count) / rate if rate > 0 else 0
            return (f"{self.label}: {self.count}/{self.total} ({pct:.0f}%) "
                    f"at {rate:.1f}/s, ~{_duration(remaining)} left")
        return f"{self.label}: {self.count} at {rate:.1f}/s after {_duration(elapsed)}"


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def format_table(rows: list[dict], columns: list[str] | None = None) -> str:
    """Render rows as an aligned text table for human-facing output.

    Deliberately minimal -- no dependency, no wrapping, no colour. Anything that
    wants richer presentation should use ``--json`` and a real tool.
    """
    if not rows:
        return "(no rows)"
    columns = columns or list(rows[0].keys())
    widths = {
        col: max(len(col), max(len(_cell(row.get(col))) for row in rows))
        for col in columns
    }
    lines = [
        "  ".join(col.ljust(widths[col]) for col in columns),
        "  ".join("-" * widths[col] for col in columns),
    ]
    lines.extend(
        "  ".join(_cell(row.get(col)).ljust(widths[col]) for col in columns)
        for row in rows
    )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
