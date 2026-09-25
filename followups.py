"""
Follow-ups and reply tracking for sent outreach.

Most cold-email replies come on the second or third touch, so every contact
whose first note was sent gets up to two short follow-ups, and anyone who
replies drops out of the sequence:

    step 1   3 days after the first note: a short bump that restates the offer
    step 2   7 days after the first note: a polite last note ("reply yes")

Follow-ups live in their own tables beside outreach_events in the campaign
database, so the first-note state machine is untouched. They follow the same
rules: drafted -> approved (by a human) -> sent (recorded by a human after they
click send), or skipped; a reply cancels any follow-up not yet sent. Nothing
here sends mail. Drafts are fixed templates built from the lead's confirmed
site gap, so they never spend model quota and never invent a new claim.

Replies can be recorded by hand (review UI "They replied") or read from Gmail
with `python cli.py sync-replies` (read-only Gmail access; see tracker_sync.py
for the one-time credentials.json setup).
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import outreach_agent as oa

# Days after the first note was sent at which each follow-up becomes due.
FOLLOWUP_SCHEDULE = {1: 3, 2: 7}
OPEN_STATES = ("drafted", "approved")
# How a confirmed site-check gap is named in a one-line follow-up.
GAP_PHRASES = (
    ("book or request an appointment", "online booking"),
    ("contact or quote form", "an after-hours inquiry form"),
    ("chat or text-us", "a text-us option"),
    ("not set up for phones", "the mobile version of your site"),
    ("secure connection", "a secure (HTTPS) connection"),
    ("footer still says", "an up-to-date site footer"),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(db_path: Path) -> sqlite3.Connection:
    oa.init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS followups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            contact_name TEXT NOT NULL,
            step INTEGER NOT NULL,
            status TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (company, contact_name, step)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS replies (
            company TEXT NOT NULL,
            contact_name TEXT NOT NULL,
            replied_at TEXT NOT NULL,
            source TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (company, contact_name)
        )"""
    )
    return conn


def sent_contacts(db_path: Path) -> list[dict]:
    """Contacts whose first note is sent, with when it was sent and what it said."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT company, contact_name, channel, message, timestamp FROM outreach_events e "
            "WHERE e.id = (SELECT MAX(id) FROM outreach_events "
            "              WHERE company = e.company AND contact_name = e.contact_name) "
            "AND e.status = ? ORDER BY e.id ASC",
            (oa.STATE_SENT,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"company": c, "contact_name": n, "channel": ch or "", "message": m or "",
         "sent_at": datetime.fromisoformat(ts)}
        for c, n, ch, m, ts in rows
    ]


def replied_keys(db_path: Path) -> dict[tuple[str, str], dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT company, contact_name, replied_at, source, snippet FROM replies")
        return {(c, n): {"replied_at": at, "source": s, "snippet": sn} for c, n, at, s, sn in rows}
    finally:
        conn.close()


def gap_phrase(target: dict | None) -> str:
    for finding in (target or {}).get("audit_findings", []):
        for fragment, phrase in GAP_PHRASES:
            if fragment in finding:
                return phrase
    return "the fix I mentioned"


def followup_text(step: int, company: str, target: dict | None) -> str:
    """Short, fixed follow-up copy. Only the confirmed gap and the company name vary."""
    fix = gap_phrase(target)
    if step == 1:
        body = (
            f"Just bumping my note from a few days ago about {fix} on your site. Happy to send "
            f"the free 2-minute screen recording showing how it would work for {company}, no "
            "strings attached. Want me to send it over?"
        )
    else:
        body = (
            f"Last note from me on this. If {fix} isn't a priority right now, no problem at all "
            "and I won't follow up again. If it is, just reply \"yes\" and I'll send the "
            "2-minute recording."
        )
    if (target or {}).get("target_type") == "small_business":
        body += "\n\n" + oa.small_business_signature()
    return body


def followup_subject(target: dict | None, contact_name: str) -> str:
    """'Re: <first note's subject>' so the follow-up reads as part of the same thread."""
    for contact in (target or {}).get("contacts", []):
        if contact.get("name") == contact_name and contact.get("subject"):
            return "Re: " + contact["subject"]
    return "Re: Quick technical note"


def generate_due_followups(
    db_path: Path, targets: dict[str, dict], now: datetime | None = None
) -> int:
    """Draft every follow-up that has come due. Idempotent; returns how many were created.

    A step is drafted only once the one before it is resolved (sent or skipped),
    so a contact never has two follow-ups waiting at the same time.
    """
    now = now or utc_now()
    replied = replied_keys(db_path)
    created = 0
    conn = _connect(db_path)
    try:
        for contact in sent_contacts(db_path):
            key = (contact["company"], contact["contact_name"])
            if key in replied:
                continue
            existing = {
                step: status for step, status in conn.execute(
                    "SELECT step, status FROM followups WHERE company = ? AND contact_name = ?", key
                )
            }
            for step, days in sorted(FOLLOWUP_SCHEDULE.items()):
                if step in existing:
                    if existing[step] in OPEN_STATES or existing[step] == "cancelled":
                        break
                    continue
                if step > 1 and existing.get(step - 1) not in ("sent", "skipped"):
                    break
                due_at = contact["sent_at"] + timedelta(days=days)
                if now < due_at:
                    break
                target = targets.get(contact["company"])
                stamp = now.isoformat()
                conn.execute(
                    "INSERT INTO followups (company, contact_name, step, status, subject, message, "
                    "due_at, created_at, updated_at) VALUES (?, ?, ?, 'drafted', ?, ?, ?, ?, ?)",
                    (*key, step, followup_subject(target, contact["contact_name"]),
                     followup_text(step, contact["company"], target), due_at.isoformat(),
                     stamp, stamp),
                )
                created += 1
                break
        conn.commit()
    finally:
        conn.close()
    return created


def list_followups(db_path: Path, statuses: tuple[str, ...]) -> list[dict]:
    conn = _connect(db_path)
    try:
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT company, contact_name, step, status, subject, message, due_at, updated_at "
            f"FROM followups WHERE status IN ({marks}) ORDER BY due_at ASC", statuses,
        ).fetchall()
    finally:
        conn.close()
    keys = ("company", "contact_name", "step", "status", "subject", "message", "due_at", "updated_at")
    return [dict(zip(keys, row)) for row in rows]


_FOLLOWUP_TRANSITIONS = {"drafted": {"approved", "skipped"}, "approved": {"sent", "skipped"}}


def set_followup_status(
    db_path: Path, company: str, contact_name: str, step: int, status: str, message: str | None = None
) -> None:
    """Move one follow-up forward: drafted -> approved|skipped, approved -> sent|skipped."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM followups WHERE company = ? AND contact_name = ? AND step = ?",
            (company, contact_name, step),
        ).fetchone()
        if row is None:
            raise LookupError(f"no follow-up #{step} for {company!r}")
        if status not in _FOLLOWUP_TRANSITIONS.get(row[0], set()):
            raise ValueError(f"follow-up #{step} for {company} is {row[0]}; cannot mark it {status}")
        if status == "approved" and message is not None and not message.strip():
            raise ValueError("the message is empty")
        conn.execute(
            "UPDATE followups SET status = ?, message = COALESCE(?, message), updated_at = ? "
            "WHERE company = ? AND contact_name = ? AND step = ?",
            (status, message, utc_now().isoformat(), company, contact_name, step),
        )
        conn.commit()
    finally:
        conn.close()


def record_reply(
    db_path: Path, company: str, contact_name: str, source: str = "manual", snippet: str = "",
    replied_at: str | None = None,
) -> bool:
    """Mark a contact as replied and cancel their unsent follow-ups. False if already recorded."""
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO replies (company, contact_name, replied_at, source, snippet) "
            "VALUES (?, ?, ?, ?, ?)",
            (company, contact_name, replied_at or utc_now().isoformat(), source, snippet[:500]),
        )
        inserted = cursor.rowcount > 0
        conn.execute(
            f"UPDATE followups SET status = 'cancelled', updated_at = ? WHERE company = ? AND "
            f"contact_name = ? AND status IN ({','.join('?' * len(OPEN_STATES))})",
            (utc_now().isoformat(), company, contact_name, *OPEN_STATES),
        )
        conn.commit()
        return inserted
    finally:
        conn.close()


def followup_contact(target: dict | None, contact_name: str, subject: str) -> dict:
    for contact in (target or {}).get("contacts", []):
        if contact.get("name") == contact_name:
            return {**contact, "subject": subject}
    return {"name": contact_name, "channel": "email", "subject": subject}


# ---------------------------------------------------------------------------
# Gmail reply sync (read-only)
# ---------------------------------------------------------------------------
def find_gmail_replies(service, email: str, since: datetime) -> dict | None:
    """The newest message from `email` received after `since`, as {replied_at, snippet}."""
    query = f"from:{email} after:{int(since.timestamp())}"
    listing = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    messages = listing.get("messages") or []
    if not messages:
        return None
    message = service.users().messages().get(
        userId="me", id=messages[0]["id"], format="metadata"
    ).execute()
    received = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, timezone.utc)
    return {"replied_at": received.isoformat(), "snippet": message.get("snippet", "")}


def sync_gmail_replies(db_path: Path, targets: dict[str, dict], service=None) -> list[str]:
    """Record a reply for every sent email contact who has written back. Returns their names."""
    if service is None:
        from tracker_sync import CREDENTIALS_PATH, TOKEN_PATH, authenticate_gmail

        if not CREDENTIALS_PATH.exists() and not TOKEN_PATH.exists():
            raise RuntimeError(
                "Gmail isn't connected yet. Create a Desktop OAuth client for the Gmail API in "
                "Google Cloud Console, save it as credentials.json in the project folder, then "
                "run sync-replies again and approve read-only access for your outreach account."
            )
        service = authenticate_gmail()
    replied = replied_keys(db_path)
    found = []
    for contact in sent_contacts(db_path):
        key = (contact["company"], contact["contact_name"])
        if key in replied:
            continue
        email = followup_contact(targets.get(contact["company"]), contact["contact_name"], "").get("email")
        if not email:
            continue
        reply = find_gmail_replies(service, email, contact["sent_at"])
        if reply and record_reply(db_path, *key, source="gmail", snippet=reply["snippet"],
                                  replied_at=reply["replied_at"]):
            found.append(contact["company"])
    return found


def cmd_sync_replies(args) -> int:
    import sys

    targets = {t["company"]: t for t in oa.load_targets(args.targets)} if args.targets.exists() else {}
    try:
        found = sync_gmail_replies(args.output, targets)
    except RuntimeError as error:
        print(f"[replies] {error}", file=sys.stderr)
        return 1
    print(f"[replies] {len(found)} new repl{'y' if len(found) == 1 else 'ies'}"
          + (": " + ", ".join(found) if found else "."))
    created = generate_due_followups(args.output, targets)
    print(f"[followups] {created} follow-up(s) now due; review them in review-ui.")
    return 0
