import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import followups as fu
import outreach_agent as oa

CONTACT = {"name": "Leadership Contact", "title": "", "channel": "email"}
TARGET = {
    "company": "Acme Dental", "target_type": "small_business",
    "audit_findings": ["no chat or text-us option on the site"],
    "contacts": [{**CONTACT, "email": "info@acme.com", "subject": "Quick fix for Acme Dental's website"}],
}


def _send_first_note(db: Path, company: str = "Acme Dental") -> datetime:
    for status in (oa.STATE_DISCOVERED, oa.STATE_DRAFTED, oa.STATE_APPROVED):
        oa.log_state(db, company, CONTACT, status, message="First note.")
    oa.mark_sent(db, company, CONTACT["name"])
    return fu.sent_contacts(db)[0]["sent_at"]


class FollowupScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "log.db"
        self.sent_at = _send_first_note(self.db)
        self.targets = {"Acme Dental": TARGET}

    def tearDown(self):
        self.tmp.cleanup()

    def _due(self, days):
        return fu.generate_due_followups(self.db, self.targets, now=self.sent_at + timedelta(days=days))

    def test_nothing_due_before_day_three(self):
        self.assertEqual(self._due(2), 0)

    def test_step_one_on_day_three_names_the_confirmed_gap_and_threads(self):
        self.assertEqual(self._due(3), 1)
        self.assertEqual(self._due(3), 0)  # idempotent
        row = fu.list_followups(self.db, ("drafted",))[0]
        self.assertEqual(row["step"], 1)
        self.assertIn("a text-us option", row["message"])
        self.assertIn('Reply "stop"', row["message"])
        self.assertEqual(row["subject"], "Re: Quick fix for Acme Dental's website")

    def test_step_two_waits_for_step_one_to_be_resolved(self):
        self._due(3)
        self.assertEqual(self._due(8), 0)  # step 1 still waiting for review
        fu.set_followup_status(self.db, "Acme Dental", CONTACT["name"], 1, "approved", "Edited bump.")
        fu.set_followup_status(self.db, "Acme Dental", CONTACT["name"], 1, "sent")
        self.assertEqual(self._due(8), 1)
        self.assertIn("Last note from me", fu.list_followups(self.db, ("drafted",))[0]["message"])
        self.assertEqual(fu.list_followups(self.db, ("sent",))[0]["message"], "Edited bump.")

    def test_follow_up_cannot_skip_approval(self):
        self._due(3)
        with self.assertRaises(ValueError):
            fu.set_followup_status(self.db, "Acme Dental", CONTACT["name"], 1, "sent")

    def test_reply_cancels_open_followups_and_stops_the_sequence(self):
        self._due(3)
        self.assertTrue(fu.record_reply(self.db, "Acme Dental", CONTACT["name"], snippet="Sure!"))
        self.assertFalse(fu.record_reply(self.db, "Acme Dental", CONTACT["name"]))
        self.assertEqual(fu.list_followups(self.db, fu.OPEN_STATES), [])
        self.assertEqual(self._due(30), 0)

    def test_gmail_sync_records_replies_from_the_lead_address(self):
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
        received = int((self.sent_at + timedelta(days=1)).timestamp() * 1000)
        messages.get.return_value.execute.return_value = {"internalDate": str(received),
                                                          "snippet": "Yes please send it"}
        found = fu.sync_gmail_replies(self.db, self.targets, service=service)
        self.assertEqual(found, ["Acme Dental"])
        query = messages.list.call_args.kwargs["q"]
        self.assertIn("from:info@acme.com", query)
        self.assertEqual(fu.replied_keys(self.db)[("Acme Dental", CONTACT["name"])]["snippet"],
                         "Yes please send it")

    def test_gap_phrase_falls_back_when_nothing_confirmed(self):
        self.assertEqual(fu.gap_phrase({"audit_findings": []}), "the fix I mentioned")


class ReviewUiFollowupTests(unittest.TestCase):
    def test_due_followup_shows_in_review_then_send_with_threaded_gmail_link(self):
        import json

        import review_ui
        with tempfile.TemporaryDirectory() as tmp:
            db, targets_path = Path(tmp) / "log.db", Path(tmp) / "targets.json"
            _send_first_note(db)
            targets_path.write_text(json.dumps([TARGET]))
            store = review_ui.ReviewStore(db, targets_path)
            fu.generate_due_followups(db, {"Acme Dental": TARGET},
                                      now=datetime.now(timezone.utc) + timedelta(days=3))
            pending = store.list_items()["pending"]
            self.assertEqual([(i["kind"], i["step"]) for i in pending if i.get("kind")], [("followup", 1)])
            store.followup("Acme Dental", CONTACT["name"], 1, "approved", "Bump.")
            approved = [i for i in store.list_items()["approved"] if i.get("kind") == "followup"]
            self.assertIn("su=Re%3A+Quick+fix", approved[0]["send_url"])
            store.replied("Acme Dental", CONTACT["name"])
            groups = store.list_items()
            self.assertEqual([i["company"] for i in groups["replied"]], ["Acme Dental"])
            self.assertEqual([i for i in groups["approved"] if i.get("kind")], [])


if __name__ == "__main__":
    unittest.main()
