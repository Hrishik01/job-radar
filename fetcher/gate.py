"""Freshness gate for the scheduled workflow: skip a run when the last
successful fetch finished less than FRESH_MINUTES ago.

    python fetcher/gate.py    # prints run=true|false (and writes it to $GITHUB_OUTPUT)

GitHub drops or delays most scheduled runs, so the workflow has many backup
slots; this keeps the extra ones cheap. Stdlib only, so it runs on the
runner's own Python before any setup step. data/jobs.json's
meta.generated_utc is written only when a fetch succeeds (a failed run
writes nothing), so it is the finish time of the last successful fetch.
Anything unreadable means "run": the gate never blocks a fetch by mistake.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

FRESH_MINUTES = 90
JOBS_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.json"


def minutes_since_last_fetch(store: dict, now: datetime) -> float | None:
    """Minutes since meta.generated_utc, or None when it's missing or unreadable."""
    try:
        finished = datetime.fromisoformat(store["meta"]["generated_utc"])
    except (KeyError, TypeError, ValueError):
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return (now - finished).total_seconds() / 60


def should_run(store: dict | None, now: datetime, fresh_minutes: int = FRESH_MINUTES) -> tuple[bool, str]:
    """(run?, reason for the log)."""
    age = minutes_since_last_fetch(store or {}, now)
    if age is None:
        return True, "no readable last-fetch time; running"
    if age < fresh_minutes:
        return False, f"last successful fetch finished {age:.0f} min ago (< {fresh_minutes}); skipping"
    return True, f"last successful fetch finished {age:.0f} min ago; running"


def main() -> int:
    try:
        store = json.loads(JOBS_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        store = None
    run, reason = should_run(store, datetime.now(timezone.utc))
    print(reason)
    print(f"run={str(run).lower()}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"run={str(run).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
