"""
Shared application history lookup.

Both main.py (single applications) and batch_runner.py (concurrent batches)
write their own logs. Neither used to read them back, so re-running a queue
re-opened and re-filled jobs that had already been prepared. This module is
the single source of truth for "have we already worked this URL?".

A job is treated as already handled only if a previous run actually reached
the review gate. Runs that ended in an error are deliberately NOT excluded,
since those represent work that never completed and is worth retrying.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# batch_runner.py writes here; statuses are success / review_gate_reached / error.
APPLIED_LOG_PATH = BASE_DIR / "queues" / "applied_log.json"
# main.py writes here; status is a free-text string ("Pending Review", "Error: ...").
APPLICATIONS_LOG_PATH = BASE_DIR / "applications_log.json"

# Statuses in applied_log.json that count as "already handled".
COMPLETED_STATUSES = {"success", "review_gate_reached"}

# Rows written by smoke_test.py are fixtures, not real applications.
SMOKE_TEST_MARKER = "example.com"

# Optional ISO-8601 timestamp. Rows logged before it are ignored for dedup
# purposes. This exists because "prepared" is not "applied": during testing the
# agent filled dozens of forms that were never submitted, and without a cutoff
# dedup would silently skip them once real applications begin. Deleting this
# file restores the full history - nothing is destroyed.
HISTORY_CUTOFF_PATH = BASE_DIR / "queues" / "history_cutoff.txt"


def load_cutoff() -> str:
    try:
        return HISTORY_CUTOFF_PATH.read_text().strip()
    except OSError:
        return ""


def normalize_url(value: str) -> str:
    """Match the normalization batch_runner.py applies when loading a queue."""
    return value.strip().rstrip("~") if isinstance(value, str) else ""


def _load(path: Path) -> list:
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def load_handled_urls() -> set[str]:
    """Return every URL a previous run carried through to the review gate."""
    handled: set[str] = set()
    cutoff = load_cutoff()

    for row in _load(APPLIED_LOG_PATH):
        if not isinstance(row, dict):
            continue
        if row.get("status") not in COMPLETED_STATUSES:
            continue
        if cutoff and str(row.get("timestamp", "")) < cutoff:
            continue
        url = normalize_url(row.get("url", ""))
        if url and SMOKE_TEST_MARKER not in url:
            handled.add(url)

    for row in _load(APPLICATIONS_LOG_PATH):
        if not isinstance(row, dict):
            continue
        # main.py logs "Pending Review" on success and "Error: ..." on failure.
        if str(row.get("status", "")).startswith("Error"):
            continue
        if cutoff and str(row.get("timestamp", "")) < cutoff:
            continue
        url = normalize_url(row.get("url", ""))
        if url and SMOKE_TEST_MARKER not in url:
            handled.add(url)

    return handled


def filter_unhandled(jobs: list[dict], verbose: bool = True) -> list[dict]:
    """Drop jobs whose URL a previous run already prepared."""
    handled = load_handled_urls()
    if not handled:
        return jobs

    kept = [job for job in jobs if normalize_url(job.get("url", "")) not in handled]
    skipped = len(jobs) - len(kept)
    if verbose and skipped:
        print(f"  [dedup] Skipped {skipped} job(s) already prepared in a previous run.")
    return kept
