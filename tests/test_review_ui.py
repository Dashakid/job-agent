import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import outreach_agent as oa
import review_ui


def _seed(tmp: Path) -> tuple[Path, Path]:
    db, targets_path = tmp / "log.db", tmp / "targets.json"
    targets = []
    for company, label, score in (("Good Dental", "high", 65), ("Big Chain", "skip", 0)):
        contact = {"name": "Leadership Contact", "title": "", "channel": "email",
                   "email": f"info@{company.split()[0].lower()}.com"}
        oa.log_state(db, company, contact, oa.STATE_DISCOVERED)
        oa.log_state(db, company, contact, oa.STATE_DRAFTED,
                     message="I noticed there's no chat on your site.", hook="reverse_audit")
        targets.append({"company": company, "website": f"https://{company.split()[0].lower()}.com",
                        "target_type": "small_business", "audit_findings": ["no chat or text-us option"],
                        "confidence": {"score": score, "label": label, "reasons": ["x"]},
                        "contacts": [contact]})
    targets_path.write_text(json.dumps(targets))
    return db, targets_path


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.targets = _seed(Path(self.tmp.name))
        self.store = review_ui.ReviewStore(self.db, self.targets)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pending_sorted_by_confidence_with_details(self):
        pending = self.store.list_items()["pending"]
        self.assertEqual([i["company"] for i in pending], ["Good Dental", "Big Chain"])
        self.assertEqual(pending[0]["send_to"], "info@good.com")
        self.assertEqual(pending[1]["blocked"], "x")

    def test_approve_moves_to_send_tab_with_gmail_link_then_sent(self):
        self.store.decide("Good Dental", "Leadership Contact", True, "Edited note.", "reverse_audit")
        groups = self.store.list_items()
        self.assertEqual([i["company"] for i in groups["approved"]], ["Good Dental"])
        self.assertIn("mail.google.com", groups["approved"][0]["send_url"])
        self.assertEqual(groups["approved"][0]["message"], "Edited note.")
        self.store.mark_sent("Good Dental", "Leadership Contact")
        self.assertEqual([i["company"] for i in self.store.list_items()["sent"]], ["Good Dental"])

    def test_skip_ranked_lead_cannot_be_approved_but_can_be_skipped(self):
        with self.assertRaises(ValueError):
            self.store.decide("Big Chain", "Leadership Contact", True, "note", "")
        self.store.decide("Big Chain", "Leadership Contact", False, "note", "")
        self.assertEqual([i["company"] for i in self.store.list_items()["skipped"]], ["Big Chain"])

    def test_approved_note_can_still_be_skipped_but_not_reapproved(self):
        self.store.decide("Good Dental", "Leadership Contact", True, "note", "")
        self.store.decide("Good Dental", "Leadership Contact", False, "note", "")
        self.assertEqual([i["company"] for i in self.store.list_items()["skipped"]],
                         ["Good Dental", "Big Chain"][:1])
        with self.assertRaises(ValueError):
            self.store.decide("Good Dental", "Leadership Contact", True, "note", "")

    def test_cannot_decide_twice(self):
        self.store.decide("Good Dental", "Leadership Contact", False, "note", "")
        with self.assertRaises(ValueError):
            self.store.decide("Good Dental", "Leadership Contact", True, "note", "")


class ServerTokenTests(unittest.TestCase):
    def test_post_without_token_is_refused_and_page_embeds_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, targets = _seed(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         review_ui.make_handler(review_ui.ReviewStore(db, targets), "tok"))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                page = urllib.request.urlopen(base + "/").read().decode()
                self.assertIn('const TOKEN = "tok"', page)
                request = urllib.request.Request(
                    base + "/api/skip", method="POST", headers={"Content-Type": "application/json"},
                    data=json.dumps({"company": "Good Dental", "contact_name": "Leadership Contact"}).encode())
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 403)
                request.add_header("X-Review-Token", "tok")
                self.assertEqual(json.loads(urllib.request.urlopen(request).read()), {"ok": True})
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
