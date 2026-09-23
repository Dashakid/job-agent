import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import batch_runner


class BatchRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_load_queue_normalizes_and_deduplicates(self):
        with tempfile.TemporaryDirectory(dir=batch_runner.BASE_DIR) as tmpdir:
            queue_path = Path(tmpdir) / "queue.json"
            queue_path.write_text(
                json.dumps(
                    [
                        {"title": "One", "url": " https://example.com/one~ "},
                        {"title": "Duplicate", "url": "https://example.com/one"},
                        {"title": "Two", "url": "https://example.com/two~~~"},
                        {"title": "Missing"},
                    ]
                ),
                encoding="utf-8",
            )
            jobs = batch_runner.load_queue(queue_path)

        self.assertEqual(
            [job["url"] for job in jobs],
            ["https://example.com/one", "https://example.com/two"],
        )

    async def test_self_heal_action_retries_and_logs_each_failure(self):
        page = AsyncMock()
        attempts = 0

        async def action():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"failure {attempts}")
            return "ready"

        with patch("batch_runner.diagnose_failure", new=AsyncMock()) as diagnose:
            result = await batch_runner.self_heal_action(
                page, "Fill", action, "https://example.com", max_retries=3
            )

        self.assertEqual(result, "ready")
        self.assertEqual(attempts, 3)
        self.assertEqual(diagnose.await_count, 2)
        self.assertEqual(page.wait_for_timeout.await_count, 2)

    async def test_applied_log_keeps_concurrent_status_records(self):
        with tempfile.TemporaryDirectory(dir=batch_runner.BASE_DIR) as tmpdir:
            log_path = Path(tmpdir) / "applied_log.json"
            jobs = [
                {"url": f"https://example.com/{index}", "title": str(index)}
                for index in range(10)
            ]
            with patch.object(batch_runner, "APPLIED_LOG_PATH", log_path):
                await asyncio.gather(
                    *(batch_runner.log_status(job, "review_gate_reached") for job in jobs)
                )
            records = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(len(records), 10)
        self.assertEqual({record["url"] for record in records}, {job["url"] for job in jobs})
        self.assertTrue(all(record["status"] == "review_gate_reached" for record in records))

    def test_is_location_label_rejects_questions_that_mention_location(self):
        self.assertTrue(batch_runner.is_location_label("Location"))
        self.assertTrue(batch_runner.is_location_label("Location (City)"))
        self.assertTrue(batch_runner.is_location_label(""))
        self.assertFalse(batch_runner.is_location_label(
            "If you're not authorized to work at the stated location, "
            "what sponsorship would you require for the role?"
        ))
        self.assertFalse(batch_runner.is_location_label(
            "Are you authorized to work in this location?"
        ))

    def test_question_patterns_match_real_form_labels(self):
        br = batch_runner
        self.assertTrue(br.COUNTRY_QUESTION_RE.search("What country are you based in?*"))
        self.assertTrue(br.PRIOR_EMPLOYMENT_RE.search(
            "Do you currently, or have you previously, worked at Capital One"))
        self.assertTrue(br.JOB_SOURCE_OPTION_RE.search("Career Page"))
        self.assertTrue(br.ACKNOWLEDGE_QUESTION_RE.search(
            "Do you consent to Brex processing your personal information for the purpose"))
        relocation = ("Do you currently live in, or plan to relocate to, the specified "
                      "location to meet this in-office requirement?")
        in_office = ("This role requires in-office work three days per week (Mon, Wed, "
                     "Thurs). Do you acknowledge and agree?")
        self.assertTrue(br.RELOCATION_QUESTION_RE.search(relocation))
        # In-office answering excludes relocation-worded questions.
        self.assertTrue(br.IN_OFFICE_QUESTION_RE.search(in_office))
        self.assertFalse(br.RELOCATION_QUESTION_RE.search(in_office))
        self.assertTrue(br.RELOCATE_OPTION_RE.search("Yes, I’d relocate prior to the start of the role"))
        start_rule = dict((key, rx) for rx, key in br.TEXT_QUESTION_RULES)["start_availability"]
        self.assertTrue(start_rule.search("When can you start a new role?"))
        self.assertTrue(br.LOCATION_QUESTION_RE.search("Where are you currently located?"))

    def test_auto_acknowledgements_skip_factual_and_sensitive_boxes(self):
        ok = batch_runner.is_auto_acceptable_acknowledgement
        self.assertTrue(ok("I acknowledge that I have opened, read, and understood the Arbitration Agreement."))
        self.assertTrue(ok("I confirm I have read the above."))
        self.assertFalse(ok("I certify that I am a U.S. citizen or permanent resident"))
        self.assertFalse(ok("I certify I am a U.S. person under export control regulations"))
        self.assertFalse(ok("I agree to receive SMS text message updates"))
        self.assertFalse(ok("Please send me future opportunities"))


if __name__ == "__main__":
    unittest.main()
