"""
Gmail -> Gemini application-status tracker sync.

Scans recent Gmail messages for job-application confirmation emails,
uses Gemini (gemini-2.5-flash) to extract structured {company, role, status}
JSON from each email snippet, and appends new entries to the same
applications_log.json / applications_log.csv files written by main.py.

Setup:
    pip install google-genai google-api-python-client google-auth-httplib2 google-auth-oauthlib
    Place an OAuth client secrets file at ./credentials.json (Gmail API,
    Desktop app type). On first run a browser window opens for consent and
    the resulting token is cached to ./token.json.

Run:
    python tracker_sync.py
"""

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai
from google.genai import types

from sheets_sync import sync_logs_to_sheet

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"
LOG_JSON_PATH = BASE_DIR / "applications_log.json"
LOG_CSV_PATH = BASE_DIR / "applications_log.csv"
CSV_FIELDNAMES = ["timestamp", "url", "company", "status", "role"]

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GEMINI_MODEL = "gemini-2.5-flash"

CONFIRMATION_KEYWORDS = [
    "application received",
    "successfully submitted",
    "confirmation",
    "thank you for applying",
    "we received your application",
]


# ---------------------------------------------------------------------------
# Gmail auth + search
# ---------------------------------------------------------------------------
def authenticate_gmail():
    """OAuth via credentials.json, caching/refreshing the token in token.json."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_PATH.name}. Download an OAuth client "
                    "secrets file (Desktop app) from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def search_confirmation_emails(service, max_results: int = 25) -> list[dict]:
    """Search recent mail for job-application confirmation keywords."""
    keyword_query = " OR ".join(f'"{kw}"' for kw in CONFIRMATION_KEYWORDS)
    query = f"newer_than:30d ({keyword_query})"

    results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    message_refs = results.get("messages", [])

    messages = []
    for ref in message_refs:
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "id": msg["id"],
            "snippet": msg.get("snippet", ""),
            "subject": headers.get("Subject", ""),
            "sender": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "permalink": f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
        })
    return messages


# ---------------------------------------------------------------------------
# Gemini extraction
# ---------------------------------------------------------------------------
def extract_application_details(client: genai.Client, message: dict) -> dict | None:
    """Ask Gemini to pull {company, role, status} out of an email snippet."""
    prompt = (
        "You are parsing a job-application confirmation email. Extract the "
        "company name, the job role/title (if mentioned, else empty string), "
        "and the application status as a short phrase (e.g. 'Application Received', "
        "'Submitted', 'Under Review').\n\n"
        f"Subject: {message['subject']}\n"
        f"From: {message['sender']}\n"
        f"Snippet: {message['snippet']}\n"
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["company", "role", "status"],
            },
        ),
    )

    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Log read/write (shared schema with main.py)
# ---------------------------------------------------------------------------
def load_existing_urls() -> set[str]:
    """Collect URLs already present in applications_log.json for dedup."""
    if not LOG_JSON_PATH.exists():
        return set()
    try:
        with open(LOG_JSON_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return set()
    return {record.get("url") for record in records if record.get("url")}


def append_log_entry(record: dict) -> None:
    """Append one record to applications_log.json and applications_log.csv."""
    records = []
    if LOG_JSON_PATH.exists():
        try:
            with open(LOG_JSON_PATH, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (json.JSONDecodeError, ValueError):
            records = []
    records.append(record)
    with open(LOG_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    write_header = not LOG_CSV_PATH.exists()
    with open(LOG_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow({field: record.get(field, "") for field in CSV_FIELDNAMES})


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def sync_inbox_applications():
    gmail_service = authenticate_gmail()
    genai_client = genai.Client()

    messages = search_confirmation_emails(gmail_service)
    print(f"Found {len(messages)} candidate email(s).")

    existing_urls = load_existing_urls()
    added = 0

    for message in messages:
        if message["permalink"] in existing_urls:
            continue

        details = extract_application_details(genai_client, message)
        if not details or not details.get("company"):
            print(f"  [skip] Could not extract details for message {message['id']}")
            continue

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": message["permalink"],
            "company": details.get("company", ""),
            "status": details.get("status", "Pending Review"),
            "role": details.get("role", ""),
        }
        append_log_entry(record)
        existing_urls.add(record["url"])
        added += 1
        print(f"  [ok] Logged {record['company']} ({record['role']}) - {record['status']}")

    print(f"Done. Added {added} new entr{'y' if added == 1 else 'ies'}.")

    if added:
        try:
            sync_logs_to_sheet()
        except Exception as exc:
            print(f"  [warn] Google Sheet sync skipped: {exc}")


if __name__ == "__main__":
    sync_inbox_applications()
