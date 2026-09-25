"""
Local web UI for reviewing outreach drafts (the browser version of outreach-review).

Serves one page on 127.0.0.1 that lists every draft most-confident first, with
the site link, the browser-confirmed gaps, and live warnings beside an editable
draft. Approve / Skip / Redraft go through the same state machine as the
terminal gate (outreach_agent.log_state), so the two can be used
interchangeably on one campaign database.

Nothing is ever sent from here. For an approved draft, "Open in Gmail" opens a
pre-filled compose window in your own browser for you to send, and "I sent it"
records the send (the same as `outreach-mark-sent`).

Run:
    python cli.py review-ui --targets queues/smb_targets.json --output queues/smb_outreach.db
"""

import argparse
import json
import secrets
import sqlite3
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import followups as fu
import outreach_agent as oa
from candidate_answers import load_candidate_context

BASE_DIR = Path(__file__).resolve().parent
PAGE_PATH = BASE_DIR / "review_ui.html"
DEFAULT_PORT = 8765
# Tabs in the UI, keyed to the latest status of each contact.
STATUS_GROUPS = {
    "pending": (oa.STATE_DRAFTED,),
    "approved": (oa.STATE_APPROVED, *oa.TAB_OPENED_STATES),
    "sent": (oa.STATE_SENT,),
    "skipped": (oa.STATE_SKIPPED,),
}
FOLLOWUP_GROUPS = {"pending": ("drafted",), "approved": ("approved",)}


class ReviewStore:
    """Reads the campaign log and targets file; writes only through outreach_agent."""

    def __init__(self, db_path: Path, targets_path: Path):
        self.db_path = db_path
        self.targets_path = targets_path
        self._lock = threading.Lock()
        try:
            self.candidate_context = load_candidate_context()
        except FileNotFoundError:
            self.candidate_context = ""
        oa.init_db(db_path)

    def _targets(self) -> dict[str, dict]:
        # Re-read every request so a `find-leads --rescore` shows up on refresh.
        if not self.targets_path.exists():
            return {}
        return {t["company"]: t for t in oa.load_targets(self.targets_path)}

    def _latest_records(self) -> list[tuple[oa.OutreachRecord, str]]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = conn.execute(
                f"SELECT {oa._RECORD_COLUMNS}, timestamp FROM outreach_events e "
                "WHERE e.id = (SELECT MAX(id) FROM outreach_events "
                "              WHERE company = e.company AND contact_name = e.contact_name) "
                "ORDER BY e.id ASC"
            ).fetchall()
        finally:
            conn.close()
        return [(oa.OutreachRecord(*(v or "" for v in row[:-1])), row[-1]) for row in rows]

    def _contact(self, record: oa.OutreachRecord, target: dict | None) -> dict:
        base = {"name": record.contact_name, "title": record.contact_title,
                "channel": record.channel}
        for contact in (target or {}).get("contacts", []):
            if contact.get("name") == record.contact_name:
                return {**base, **contact}
        return {**base, "profile_url": record.url}

    def _warnings(self, message: str, record: oa.OutreachRecord, target: dict | None) -> list[str]:
        warnings = oa.draft_problems(message, record.source_text, self.candidate_context)
        if target:
            warnings += oa.unconfirmed_claims(message, target)
        return warnings

    def _block_reason(self, record: oa.OutreachRecord, target: dict | None, contact: dict) -> str:
        return oa.contact_handle_problem(contact) or (oa.review_block_reason(target) if target else "")

    def _item(self, record: oa.OutreachRecord, timestamp: str, target: dict | None) -> dict:
        contact = self._contact(record, target)
        confidence = (target or {}).get("confidence") or {}
        send_url = ""
        if record.status in STATUS_GROUPS["approved"]:
            try:
                send_url, _ = oa.build_outreach_url(contact, record.message)
            except ValueError:
                send_url = ""
        return {
            "company": record.company,
            "contact_name": record.contact_name,
            "channel": record.channel,
            "status": record.status,
            "hook": record.hook,
            "message": record.message,
            "updated": timestamp,
            "website": (target or {}).get("website", ""),
            "category": (target or {}).get("category", ""),
            "confidence": confidence,
            "confirmed": (target or {}).get("audit_findings", []),
            "disproven": (target or {}).get("disproven_findings", []),
            "browser_checked": bool((target or {}).get("browser_checked")),
            "send_to": contact.get("email") or contact.get("contact_form_url")
            or contact.get("profile_url", ""),
            "send_url": send_url,
            "blocked": self._block_reason(record, target, contact),
            "warnings": self._warnings(record.message, record, target) if record.message else [],
            "can_redraft": bool(target and self.candidate_context),
        }

    def _followup_item(self, row: dict, target: dict | None) -> dict:
        contact = fu.followup_contact(target, row["contact_name"], row["subject"])
        send_url = ""
        if row["status"] == "approved":
            try:
                send_url, _ = oa.build_outreach_url(contact, row["message"])
            except ValueError:
                send_url = ""
        return {
            "kind": "followup", "step": row["step"], "company": row["company"],
            "contact_name": row["contact_name"], "channel": contact.get("channel", "email"),
            "status": row["status"], "hook": f"follow-up #{row['step']}", "message": row["message"],
            "subject": row["subject"], "updated": row["updated_at"], "due_at": row["due_at"],
            "website": (target or {}).get("website", ""), "category": (target or {}).get("category", ""),
            "confidence": (target or {}).get("confidence") or {},
            "confirmed": (target or {}).get("audit_findings", []),
            "disproven": (target or {}).get("disproven_findings", []),
            "browser_checked": bool((target or {}).get("browser_checked")),
            "send_to": contact.get("email") or contact.get("contact_form_url", ""),
            "send_url": send_url, "blocked": "", "warnings": [], "can_redraft": False,
        }

    def list_items(self) -> dict:
        targets = self._targets()
        with self._lock:
            fu.generate_due_followups(self.db_path, targets)
        replied = fu.replied_keys(self.db_path)
        open_followups = {
            (row["company"], row["contact_name"]): row
            for row in fu.list_followups(self.db_path, fu.OPEN_STATES)
        }
        groups: dict[str, list[dict]] = {name: [] for name in (*STATUS_GROUPS, "replied")}
        for record, timestamp in self._latest_records():
            key = (record.company, record.contact_name)
            if key in replied:
                item = self._item(record, timestamp, targets.get(record.company))
                item["reply"] = replied[key]
                groups["replied"].append(item)
                continue
            for name, statuses in STATUS_GROUPS.items():
                if record.status in statuses:
                    item = self._item(record, timestamp, targets.get(record.company))
                    if name == "sent":
                        item["next_followup"] = self._next_followup(record, timestamp, open_followups.get(key))
                    groups[name].append(item)
        for name, statuses in FOLLOWUP_GROUPS.items():
            for row in fu.list_followups(self.db_path, statuses):
                if (row["company"], row["contact_name"]) not in replied:
                    groups[name].append(self._followup_item(row, targets.get(row["company"])))
        for items in groups.values():
            items.sort(key=lambda item: -(item["confidence"].get("score") or 0))
        return groups

    def _next_followup(self, record, sent_timestamp: str, open_row: dict | None) -> str:
        if open_row:
            return f"follow-up #{open_row['step']} is {'waiting to send' if open_row['status'] == 'approved' else 'ready to review'}"
        from datetime import datetime, timedelta

        conn = sqlite3.connect(str(self.db_path))
        try:
            done = {step for (step,) in conn.execute(
                "SELECT step FROM followups WHERE company = ? AND contact_name = ?",
                (record.company, record.contact_name))}
        finally:
            conn.close()
        for step, days in sorted(fu.FOLLOWUP_SCHEDULE.items()):
            if step not in done:
                due = datetime.fromisoformat(sent_timestamp) + timedelta(days=days)
                return f"follow-up #{step} due {due.strftime('%b %d')}"
        return "sequence finished"

    def _find(self, company: str, contact_name: str) -> tuple[oa.OutreachRecord, dict | None]:
        record = oa.latest_outreach_record(self.db_path, company, contact_name)
        if record is None:
            raise LookupError(f"no outreach record for {company!r}")
        return record, self._targets().get(company)

    def check(self, company: str, contact_name: str, message: str) -> list[str]:
        record, target = self._find(company, contact_name)
        return self._warnings(message, record, target)

    def decide(self, company: str, contact_name: str, approve: bool, message: str, hook: str) -> None:
        with self._lock:
            record, target = self._find(company, contact_name)
            # Skipping is also allowed for an approved note that hasn't been sent yet.
            allowed = (oa.STATE_DRAFTED,) if approve else (oa.STATE_DRAFTED, *STATUS_GROUPS["approved"])
            if record.status not in allowed:
                raise ValueError(f"this note is already {record.status}")
            contact = self._contact(record, target)
            if approve:
                blocked = self._block_reason(record, target, contact)
                if blocked:
                    raise ValueError(f"approval disabled: {blocked}")
                if not message.strip():
                    raise ValueError("the message is empty")
            status = oa.STATE_APPROVED if approve else oa.STATE_SKIPPED
            contact_row = {"name": record.contact_name, "title": record.contact_title,
                           "channel": record.channel}
            oa.log_state(self.db_path, company, contact_row, status,
                         message=message, hook=hook or record.hook)

    def redraft(self, company: str, contact_name: str, hook: str) -> dict:
        record, target = self._find(company, contact_name)
        if not (target and self.candidate_context):
            raise ValueError("redraft needs this lead in the targets file and candidate_context.md")
        new_hook = oa.next_hook(record.hook or hook) if hook == "next" else (hook or record.hook)
        site_excerpt = "\n".join(
            line for line in record.source_text.splitlines()
            if "Site check of their public website found" not in line
        )
        message = oa.draft_outreach_message(
            company, self._contact(record, target), [], self.candidate_context,
            target.get("context", ""), new_hook, site_excerpt=site_excerpt,
            target_type=oa.classify_target_type(target),
        )
        if not message:
            raise RuntimeError("drafting is unavailable right now (no key, or both providers "
                               "are rate-limited); try again in a minute")
        return {"message": message, "hook": new_hook,
                "warnings": self._warnings(message, record, target)}

    def followup(self, company: str, contact_name: str, step: int, status: str, message: str | None) -> None:
        with self._lock:
            fu.set_followup_status(self.db_path, company, contact_name, int(step), status, message)

    def replied(self, company: str, contact_name: str) -> None:
        with self._lock:
            fu.record_reply(self.db_path, company, contact_name, source="manual")

    def sync_replies(self) -> dict:
        found = fu.sync_gmail_replies(self.db_path, self._targets())
        return {"found": found}

    def mark_sent(self, company: str, contact_name: str) -> None:
        with self._lock:
            if not oa.mark_sent(self.db_path, company, contact_name):
                raise LookupError(f"no approved draft for {company!r}")


def make_handler(store: ReviewStore, token: str):
    page = PAGE_PATH.read_text(encoding="utf-8").replace("__REVIEW_TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/items":
                self._json(HTTPStatus.OK, store.list_items())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            # Another website can make the browser POST to localhost, but it
            # cannot set a custom header without a CORS preflight we never allow.
            if self.headers.get("X-Review-Token") != token:
                self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                company, contact = body.get("company", ""), body.get("contact_name", "")
                if self.path == "/api/approve":
                    store.decide(company, contact, True, body.get("message", ""), body.get("hook", ""))
                    result = {"ok": True}
                elif self.path == "/api/skip":
                    store.decide(company, contact, False, body.get("message", ""), body.get("hook", ""))
                    result = {"ok": True}
                elif self.path == "/api/redraft":
                    result = store.redraft(company, contact, body.get("hook", ""))
                elif self.path == "/api/check":
                    result = {"warnings": store.check(company, contact, body.get("message", ""))}
                elif self.path == "/api/sent":
                    store.mark_sent(company, contact)
                    result = {"ok": True}
                elif self.path in ("/api/followup/approve", "/api/followup/skip", "/api/followup/sent"):
                    status = {"approve": "approved", "skip": "skipped", "sent": "sent"}[self.path.rsplit("/", 1)[1]]
                    message = body.get("message") if status == "approved" else None
                    store.followup(company, contact, body.get("step", 0), status, message)
                    result = {"ok": True}
                elif self.path == "/api/replied":
                    store.replied(company, contact)
                    result = {"ok": True}
                elif self.path == "/api/sync-replies":
                    result = store.sync_replies()
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
            except (ValueError, LookupError, RuntimeError, oa.InvalidTransitionError) as error:
                self._json(HTTPStatus.CONFLICT, {"error": str(error)})
                return
            self._json(HTTPStatus.OK, result)

    return Handler


def serve(targets: Path, output: Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    store = ReviewStore(output, targets)
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token))
    url = f"http://127.0.0.1:{port}/"
    print(f"[review-ui] Reviewing {output.name} at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[review-ui] Stopped.")
    finally:
        server.server_close()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", type=Path, default=oa.DEFAULT_TARGETS_PATH)
    parser.add_argument("--output", type=Path, default=oa.DEFAULT_DB_PATH)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")


def cmd_review_ui(args: argparse.Namespace) -> int:
    try:
        serve(args.targets, args.output, args.port, open_browser=not args.no_browser)
    except OSError as error:
        print(f"[error] review-ui could not start on port {args.port}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local web UI for outreach draft review")
    add_arguments(parser)
    sys.exit(cmd_review_ui(parser.parse_args()))
