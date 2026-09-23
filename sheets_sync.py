"""
Sync applications_log.json to a Google Sheet named 'Job Application Tracker'.

Uses a service account (service_account.json) via google-auth + gspread.
Exposes sync_logs_to_sheet() so it can be imported and called from both
main.py (after each Playwright application) and tracker_sync.py (after each
Gmail-derived entry).
"""

import json
import sqlite3
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = Path(__file__).resolve().parent
SERVICE_ACCOUNT_PATH = BASE_DIR / "service_account.json"
LOG_JSON_PATH = BASE_DIR / "applications_log.json"

SHEET_NAME = "Job Application Tracker"
WORKSHEET_TITLE = "Applications"
HEADERS = ["Job Title", "Company", "Date Applied", "Status", "Notes"]

# Same spreadsheet, separate tab: outreach campaigns are a different workflow
# (outreach_agent.py) from job-board applications and must not share rows.
OUTREACH_WORKSHEET_TITLE = "Outreach"
OUTREACH_HEADERS = ["Company", "Contact", "Title", "Channel", "Status", "Timestamp", "Notes"]
DEFAULT_OUTREACH_DB_PATH = BASE_DIR / "queues" / "outreach_log.db"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_client() -> gspread.Client:
    if not SERVICE_ACCOUNT_PATH.exists():
        raise FileNotFoundError(
            f"Missing {SERVICE_ACCOUNT_PATH.name}. Download a service account "
            "key JSON from Google Cloud Console and share the sheet with its "
            "client_email."
        )
    creds = Credentials.from_service_account_file(str(SERVICE_ACCOUNT_PATH), scopes=SCOPES)
    return gspread.authorize(creds)


def _open_worksheet(client: gspread.Client) -> gspread.Worksheet:
    """Open the tracker spreadsheet (creating it if missing) and its worksheet."""
    try:
        spreadsheet = client.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.sheet1
        worksheet.update_title(WORKSHEET_TITLE)

    existing_header = worksheet.row_values(1)
    if existing_header != HEADERS:
        worksheet.update("A1", [HEADERS])

    return worksheet


def _load_local_log() -> list[dict]:
    if not LOG_JSON_PATH.exists():
        return []
    try:
        with open(LOG_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def _record_to_row(record: dict) -> list[str]:
    return [
        record.get("role", ""),
        record.get("company", ""),
        record.get("timestamp", ""),
        record.get("status", ""),
        record.get("url", ""),
    ]


def sync_logs_to_sheet() -> int:
    """
    Append any applications_log.json entries missing from the tracker sheet.
    Dedup key: the "Notes" column, which stores each entry's unique URL.
    Returns the number of new rows appended.
    """
    records = _load_local_log()
    if not records:
        print("No local log entries to sync.")
        return 0

    client = _get_client()
    worksheet = _open_worksheet(client)

    existing_rows = worksheet.get_all_records()
    existing_notes = {str(row.get("Notes", "")) for row in existing_rows}

    new_rows = [
        _record_to_row(record)
        for record in records
        if record.get("url", "") not in existing_notes
    ]

    if not new_rows:
        print("Google Sheet already up to date.")
        return 0

    worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"Appended {len(new_rows)} new row(s) to '{SHEET_NAME}'.")
    return len(new_rows)


def _open_outreach_worksheet(client: gspread.Client) -> gspread.Worksheet:
    """Open the tracker spreadsheet's Outreach tab (creating it if missing)."""
    try:
        spreadsheet = client.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(OUTREACH_WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=OUTREACH_WORKSHEET_TITLE, rows=1000, cols=len(OUTREACH_HEADERS)
        )

    existing_header = worksheet.row_values(1)
    if existing_header != OUTREACH_HEADERS:
        worksheet.update("A1", [OUTREACH_HEADERS])

    return worksheet


def _load_outreach_events(db_path: Path) -> list[dict]:
    """Read every outreach_events row (one per state transition) from the local SQLite log."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT company, contact_name, contact_title, channel, status, timestamp "
            "FROM outreach_events ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def sync_outreach_to_sheet(db_path: Path = DEFAULT_OUTREACH_DB_PATH) -> int:
    """
    Append any outreach_events rows missing from the tracker sheet's Outreach tab.

    Dedup key: a composite of company/contact/status/timestamp packed into the
    "Notes" column, mirroring the URL-as-dedup-key pattern used for applications.
    """
    records = _load_outreach_events(Path(db_path))
    if not records:
        print("No local outreach events to sync.")
        return 0

    client = _get_client()
    worksheet = _open_outreach_worksheet(client)

    existing_rows = worksheet.get_all_records()
    existing_notes = {str(row.get("Notes", "")) for row in existing_rows}

    new_rows = []
    for record in records:
        note_key = (
            f"{record.get('company', '')}|{record.get('contact_name', '')}|"
            f"{record.get('status', '')}|{record.get('timestamp', '')}"
        )
        if note_key in existing_notes:
            continue
        new_rows.append([
            record.get("company", ""),
            record.get("contact_name", ""),
            record.get("contact_title", ""),
            record.get("channel", ""),
            record.get("status", ""),
            record.get("timestamp", ""),
            note_key,
        ])

    if not new_rows:
        print("Outreach sheet already up to date.")
        return 0

    worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"Appended {len(new_rows)} new outreach row(s) to '{SHEET_NAME}'.")
    return len(new_rows)


if __name__ == "__main__":
    sync_logs_to_sheet()
