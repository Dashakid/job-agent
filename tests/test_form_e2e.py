"""
End-to-end check of batch_runner.prepare_job against a local sample form.

Runs headless Chromium on tests/fixtures/sample_form.html with the example
profile. No network, no Gemini calls, and every log write is redirected to a
temp directory. Skipped when Playwright's Chromium is not installed.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

import batch_runner

ROOT = Path(__file__).resolve().parent.parent
FORM_URL = (Path(__file__).resolve().parent / "fixtures" / "sample_form.html").as_uri()

FORM_VALUES_JS = """() => ({
  first: first_name.value, last: last_name.value, email: email.value,
  phone: phone.value, linkedin: linkedin.value,
  resume: resume.files.length ? resume.files[0].name : '',
  auth: auth.value, spons: spons.value, why: why.value, salary: salary.value, loc_sponsor: loc_sponsor.value,
  consent: consent.checked, submitted: !!window.__submitted,
})"""


class SampleFormTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_job_fills_form_and_never_submits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            resume = tmp / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4\n%%EOF\n")
            profile = json.loads((ROOT / "profile.example.json").read_text(encoding="utf-8"))
            profile["resume_path"] = str(resume)
            env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}

            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(batch_runner, "APPLIED_LOG_PATH", tmp / "applied_log.json"),
                patch.object(batch_runner, "SELF_HEAL_LOG_PATH", tmp / "self_heal_log.json"),
                patch.object(batch_runner, "FAILURE_DIR", tmp / "failures"),
                patch.object(
                    batch_runner, "PREPARED_ANSWERS_PATH", ROOT / "prepared_answers.example.json"
                ),
            ):
                async with async_playwright() as playwright:
                    try:
                        browser = await playwright.chromium.launch(headless=True)
                    except Exception as error:
                        self.skipTest(f"Chromium not available: {error}")
                    try:
                        context = await browser.new_context()
                        page, unfinished = await batch_runner.prepare_job(
                            context, {"url": FORM_URL, "title": "Sample"}, profile,
                            asyncio.Semaphore(1),
                        )
                        values = await page.evaluate(FORM_VALUES_JS)
                    finally:
                        await browser.close()

        self.assertIsNone(unfinished, "job should reach the review gate")
        self.assertEqual(values["first"], "Jane")
        self.assertEqual(values["last"], "Doe")
        self.assertEqual(values["email"], "jane.doe@example.com")
        self.assertEqual(values["phone"], profile["phone"])
        self.assertEqual(values["linkedin"], profile["linkedin_url"])
        self.assertEqual(values["resume"], "resume.pdf")
        # Native <select> questions answered from the profile.
        self.assertEqual(values["auth"], "Yes")
        self.assertEqual(values["spons"], "No")
        # Prepared answer matched by regex, no LLM involved.
        self.assertTrue(values["why"].startswith("Replace this with your own answer"))
        # Sensitive and legal items are left for the human.
        self.assertEqual(values["salary"], "")
        # A sponsorship question mentioning "location" is not a location field.
        self.assertEqual(values["loc_sponsor"], "")
        self.assertFalse(values["consent"])
        self.assertFalse(values["submitted"])


if __name__ == "__main__":
    unittest.main()
