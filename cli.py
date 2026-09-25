"""
Command-line interface for the job-agent repository.

Subcommands:
    apply       --url URL     Pre-fill a job application form (main.py)
    sync-email                Scan Gmail for confirmation emails (tracker_sync.py)
    sync-sheets                Push local JSON logs to Google Sheets (sheets_sync.py)
    sync-all                  Run sync-email then sync-sheets
    batch-run   --file FILE   Apply to every job URL in a scraper.py queue file
    outreach-run                 Draft, approve at the terminal, then open review tabs (outreach_agent.py)
    outreach-review              Approve, edit, or skip drafts saved by outreach-run --draft-only
    outreach-mark-sent            Record a human-confirmed outreach send
    outreach-sync-sheet            Push the outreach SQLite log to Google Sheets
    find-leads                   Find small businesses with fixable website gaps (lead_finder.py)
    review-ui                    Review outreach drafts in a local web page (review_ui.py)
    sync-replies                 Mark contacts who replied (read-only Gmail) and draft due follow-ups

Examples:
    python cli.py apply --url "https://jobs.ashbyhq.com/..."
    python cli.py sync-email
    python cli.py sync-sheets
    python cli.py sync-all
    python cli.py batch-run --file queues/pending_jobs.json
    python cli.py outreach-run --targets queues/outreach_targets.json --output queues/outreach_log.db
    python cli.py outreach-run --draft-only && python cli.py outreach-review
    python cli.py find-leads --category accountants --area "Tampa, FL"
    python cli.py outreach-run --targets queues/smb_targets.json --output queues/smb_outreach.db
    python cli.py review-ui --targets queues/smb_targets.json --output queues/smb_outreach.db
"""

import argparse
import json
import sys
from pathlib import Path


def cmd_apply(args: argparse.Namespace) -> int:
    from main import run_application_agent

    try:
        run_application_agent(args.url)
    except Exception as exc:
        print(f"[error] apply failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_email(_args: argparse.Namespace) -> int:
    from tracker_sync import sync_inbox_applications

    try:
        sync_inbox_applications()
    except Exception as exc:
        print(f"[error] sync-email failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_sheets(_args: argparse.Namespace) -> int:
    from sheets_sync import sync_logs_to_sheet

    try:
        sync_logs_to_sheet()
    except Exception as exc:
        print(f"[error] sync-sheets failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_sync_all(args: argparse.Namespace) -> int:
    print("=== Running sync-email ===")
    email_status = cmd_sync_email(args)
    print("\n=== Running sync-sheets ===")
    sheets_status = cmd_sync_sheets(args)
    return 1 if (email_status or sheets_status) else 0


def cmd_batch_run(args: argparse.Namespace) -> int:
    from main import run_application_agent

    queue_path = Path(args.file)
    if not queue_path.exists():
        print(f"[error] batch-run failed: queue file not found: {queue_path}", file=sys.stderr)
        return 1

    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[error] batch-run failed: could not parse {queue_path}: {exc}", file=sys.stderr)
        return 1

    if not jobs:
        print("No pending jobs found in queue file.")
        return 0

    failures = 0
    for i, job in enumerate(jobs, start=1):
        url = job.get("url") if isinstance(job, dict) else job
        if not url:
            continue
        print(f"\n=== [{i}/{len(jobs)}] Applying: {url} ===")
        try:
            run_application_agent(url)
        except Exception as exc:
            print(f"[error] batch-run: failed on {url}: {exc}", file=sys.stderr)
            failures += 1

    print(f"\nbatch-run complete: {len(jobs) - failures}/{len(jobs)} processed successfully.")

    try:
        from sheets_sync import sync_logs_to_sheet
        sync_logs_to_sheet()
    except Exception as exc:
        print(f"[warn] Google Sheet sync skipped: {exc}")

    return 1 if failures else 0


def cmd_review_batch(args: argparse.Namespace) -> int:
    from main import run_application_review_batch

    queue_path = Path(args.file)
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[error] review-batch failed: could not load {queue_path}: {exc}", file=sys.stderr)
        return 1

    if not jobs:
        print("No jobs selected for review.")
        return 0

    try:
        run_application_review_batch(jobs)
    except Exception as exc:
        print(f"[error] review-batch failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_outreach_run(args: argparse.Namespace) -> int:
    from outreach_agent import cmd_run

    return cmd_run(args)


def cmd_outreach_review(args: argparse.Namespace) -> int:
    from outreach_agent import cmd_review

    return cmd_review(args)


def cmd_outreach_mark_sent(args: argparse.Namespace) -> int:
    from outreach_agent import cmd_mark_sent

    return cmd_mark_sent(args)


def cmd_outreach_sync_sheet(args: argparse.Namespace) -> int:
    from outreach_agent import cmd_sync_sheet

    return cmd_sync_sheet(args)


def cmd_find_leads(args: argparse.Namespace) -> int:
    from lead_finder import cmd_find_leads as run_find_leads

    return run_find_leads(args)


def cmd_review_ui(args: argparse.Namespace) -> int:
    from review_ui import cmd_review_ui as run_review_ui

    return run_review_ui(args)


def cmd_sync_replies(args: argparse.Namespace) -> int:
    from followups import cmd_sync_replies as run_sync_replies

    return run_sync_replies(args)


def build_parser() -> argparse.ArgumentParser:
    from outreach_agent import (
        DEFAULT_DB_PATH, DEFAULT_MIN_INTERVAL_SECONDS, DEFAULT_TARGETS_PATH, DEFAULT_USER_DATA_DIR,
    )

    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="job-agent command-line interface: apply to jobs and sync application logs.",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True,
        metavar=(
            "{apply,sync-email,sync-sheets,sync-all,batch-run,review-batch,"
            "outreach-run,outreach-review,outreach-mark-sent,outreach-sync-sheet,find-leads,review-ui,sync-replies}"
        ),
    )

    apply_parser = subparsers.add_parser("apply", help="Pre-fill a job application form")
    apply_parser.add_argument("--url", required=True, help="Job application page URL")
    apply_parser.set_defaults(func=cmd_apply)

    sync_email_parser = subparsers.add_parser("sync-email", help="Scan Gmail for application confirmation emails")
    sync_email_parser.set_defaults(func=cmd_sync_email)

    sync_sheets_parser = subparsers.add_parser("sync-sheets", help="Push local JSON logs to Google Sheets")
    sync_sheets_parser.set_defaults(func=cmd_sync_sheets)

    sync_all_parser = subparsers.add_parser("sync-all", help="Run sync-email then sync-sheets")
    sync_all_parser.set_defaults(func=cmd_sync_all)

    batch_run_parser = subparsers.add_parser(
        "batch-run", help="Apply to every job URL in a scraper.py queue file"
    )
    batch_run_parser.add_argument(
        "--file", default="queues/pending_jobs.json", help="Path to the queue JSON file (default: %(default)s)"
    )
    batch_run_parser.set_defaults(func=cmd_batch_run)

    review_batch_parser = subparsers.add_parser(
        "review-batch", help="Pre-fill selected jobs in tabs and pause once for manual review"
    )
    review_batch_parser.add_argument(
        "--file", default="queues/fit_jobs.json", help="Selected job queue (default: %(default)s)"
    )
    review_batch_parser.set_defaults(func=cmd_review_batch)

    outreach_run_parser = subparsers.add_parser(
        "outreach-run", help="Draft and open review tabs for outreach targets (never sends)"
    )
    outreach_run_parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH)
    outreach_run_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    outreach_run_parser.add_argument("--concurrency", type=int, default=3)
    outreach_run_parser.add_argument(
        "--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_SECONDS
    )
    outreach_run_parser.add_argument("--user-data-dir", type=Path, default=DEFAULT_USER_DATA_DIR)
    outreach_run_parser.add_argument("--no-persistent-context", action="store_true")
    outreach_run_parser.add_argument(
        "--draft-only", action="store_true",
        help="Save drafts for outreach-review instead of reviewing them now; opens no outreach tabs",
    )
    outreach_run_parser.set_defaults(func=cmd_outreach_run)

    outreach_review_parser = subparsers.add_parser(
        "outreach-review", help="Approve, edit, or skip saved outreach drafts at the terminal"
    )
    outreach_review_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    outreach_review_parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH)
    outreach_review_parser.set_defaults(func=cmd_outreach_review)

    outreach_mark_sent_parser = subparsers.add_parser(
        "outreach-mark-sent", help="Record that a human has sent a previously drafted message"
    )
    outreach_mark_sent_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    outreach_mark_sent_parser.add_argument("--company", required=True)
    outreach_mark_sent_parser.add_argument("--contact", default="")
    outreach_mark_sent_parser.set_defaults(func=cmd_outreach_mark_sent)

    outreach_sync_sheet_parser = subparsers.add_parser(
        "outreach-sync-sheet", help="Push the outreach SQLite log to the tracker Google Sheet"
    )
    outreach_sync_sheet_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    outreach_sync_sheet_parser.set_defaults(func=cmd_outreach_sync_sheet)

    from lead_finder import add_arguments as add_find_leads_arguments

    find_leads_parser = subparsers.add_parser(
        "find-leads", help="Find small businesses with fixable website gaps and queue them for outreach"
    )
    add_find_leads_arguments(find_leads_parser)
    find_leads_parser.set_defaults(func=cmd_find_leads)

    from review_ui import add_arguments as add_review_ui_arguments

    review_ui_parser = subparsers.add_parser(
        "review-ui", help="Review outreach drafts in a local web page (never sends)"
    )
    add_review_ui_arguments(review_ui_parser)
    review_ui_parser.set_defaults(func=cmd_review_ui)

    sync_replies_parser = subparsers.add_parser(
        "sync-replies", help="Mark contacts who replied (read-only Gmail) and draft due follow-ups"
    )
    sync_replies_parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH)
    sync_replies_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    sync_replies_parser.set_defaults(func=cmd_sync_replies)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
