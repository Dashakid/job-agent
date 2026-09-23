import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import outreach_agent


def _mock_locator(*, count: int = 1, visible: bool = True, tag: str = "textarea"):
    locator = AsyncMock()
    locator.count = AsyncMock(return_value=count)
    locator.is_visible = AsyncMock(return_value=visible)
    locator.evaluate = AsyncMock(return_value=tag)
    locator.first = locator
    return locator


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_enforces_minimum_interval_between_calls(self):
        limiter = outreach_agent.RateLimiter(0.1)
        start = time.monotonic()
        await limiter.wait()
        await limiter.wait()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.1)

    async def test_zero_interval_never_sleeps(self):
        limiter = outreach_agent.RateLimiter(0.0)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await limiter.wait()
            await limiter.wait()
        sleep_mock.assert_not_awaited()

    async def test_negative_interval_is_clamped_to_zero(self):
        limiter = outreach_agent.RateLimiter(-5.0)
        self.assertEqual(limiter.min_interval_seconds, 0.0)


class SelectorSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_message_and_send_selectors_never_overlap(self):
        self.assertTrue(
            set(outreach_agent.MESSAGE_FIELD_SELECTORS).isdisjoint(
                outreach_agent.SEND_BUTTON_SELECTORS
            )
        )

    async def test_report_send_controls_detects_but_never_clicks(self):
        page = AsyncMock()
        control = _mock_locator(count=1, visible=True)
        control.click = AsyncMock()
        page.locator = MagicMock(return_value=control)

        found = await outreach_agent.report_send_controls(page)

        self.assertTrue(found)
        control.click.assert_not_called()

    async def test_report_send_controls_returns_false_when_nothing_visible(self):
        page = AsyncMock()
        control = _mock_locator(count=0, visible=False)
        page.locator = MagicMock(return_value=control)

        found = await outreach_agent.report_send_controls(page)

        self.assertFalse(found)

    async def test_fill_message_box_never_touches_send_selectors(self):
        page = AsyncMock()
        textarea = _mock_locator(count=1, visible=True, tag="textarea")
        textarea.fill = AsyncMock()
        page.locator = MagicMock(return_value=textarea)

        filled = await outreach_agent.fill_message_box(page, "hello")

        self.assertTrue(filled)
        textarea.fill.assert_awaited_once_with("hello")
        # No selector used to find a fillable field should be a send control.
        for call in page.locator.call_args_list:
            self.assertNotIn(call.args[0], outreach_agent.SEND_BUTTON_SELECTORS)

    async def test_fill_message_box_raises_when_no_field_found(self):
        page = AsyncMock()
        empty = _mock_locator(count=0, visible=False)
        page.locator = MagicMock(return_value=empty)

        with self.assertRaises(outreach_agent.NoComposerFoundError):
            await outreach_agent.fill_message_box(page, "hello")


class AuthWallDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def _page(self, *, url="https://acme.example.com/team/jane", title="Acme"):
        page = AsyncMock()
        page.url = url
        page.title = AsyncMock(return_value=title)
        page.locator = MagicMock(return_value=_mock_locator(count=0, visible=False))
        return page

    async def test_detects_login_url(self):
        page = await self._page(url="https://www.linkedin.com/login?session=1")
        self.assertTrue(await outreach_agent.detect_auth_wall(page))

    async def test_detects_signup_title(self):
        page = await self._page(title="Sign up for X to continue")
        self.assertTrue(await outreach_agent.detect_auth_wall(page))

    async def test_detects_password_field_even_with_clean_url_and_title(self):
        page = await self._page()
        password_field = _mock_locator(count=1, visible=True)
        page.locator = MagicMock(
            side_effect=lambda selector: password_field
            if selector == "input[type='password']" else _mock_locator(count=0, visible=False)
        )
        self.assertTrue(await outreach_agent.detect_auth_wall(page))

    async def test_returns_false_for_a_clean_public_page(self):
        page = await self._page()
        self.assertFalse(await outreach_agent.detect_auth_wall(page))

    async def test_tolerates_errors_reading_url_or_title(self):
        page = AsyncMock()
        page.title = AsyncMock(side_effect=RuntimeError("boom"))
        page.locator = MagicMock(return_value=_mock_locator(count=0, visible=False))
        # page.url access itself must not raise for a real Page; simulate by
        # making the attribute a property-like object that raises on str().
        page.url = "https://acme.example.com"
        self.assertFalse(await outreach_agent.detect_auth_wall(page))


class LaunchBrowserContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_persistent_context_when_launch_succeeds(self):
        persistent_context = AsyncMock()
        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(return_value=persistent_context)

        with tempfile.TemporaryDirectory() as tmpdir:
            user_data_dir = Path(tmpdir) / "profile"
            browser, context = await outreach_agent.launch_browser_context(playwright, user_data_dir)

        self.assertIsNone(browser)
        self.assertIs(context, persistent_context)
        playwright.chromium.launch.assert_not_called()

    async def test_falls_back_to_fresh_context_when_persistent_launch_fails(self):
        fallback_browser = AsyncMock()
        fallback_context = AsyncMock()
        fallback_browser.new_context = AsyncMock(return_value=fallback_context)

        playwright = AsyncMock()
        playwright.chromium.launch_persistent_context = AsyncMock(
            side_effect=RuntimeError("profile locked")
        )
        playwright.chromium.launch = AsyncMock(return_value=fallback_browser)

        with tempfile.TemporaryDirectory() as tmpdir:
            user_data_dir = Path(tmpdir) / "profile"
            browser, context = await outreach_agent.launch_browser_context(playwright, user_data_dir)

        self.assertIs(browser, fallback_browser)
        self.assertIs(context, fallback_context)

    async def test_none_user_data_dir_always_uses_a_fresh_context(self):
        fallback_browser = AsyncMock()
        fallback_context = AsyncMock()
        fallback_browser.new_context = AsyncMock(return_value=fallback_context)

        playwright = AsyncMock()
        playwright.chromium.launch = AsyncMock(return_value=fallback_browser)

        browser, context = await outreach_agent.launch_browser_context(playwright, None)

        self.assertIs(browser, fallback_browser)
        self.assertIs(context, fallback_context)
        playwright.chromium.launch_persistent_context.assert_not_called()


class TechSignalTests(unittest.TestCase):
    def test_extract_tech_signals_matches_case_insensitively_and_dedupes(self):
        text = "We run python and Python and FastAPI on Docker with postgres."
        signals = outreach_agent.extract_tech_signals(text)
        # Order-preserving de-dup on the raw matched token, not the canonical keyword.
        self.assertIn("FastAPI", signals)
        self.assertIn("Docker", signals)
        self.assertTrue(any(s.lower() == "python" for s in signals))
        self.assertTrue(any(s.lower() == "postgres" for s in signals))

    def test_extract_tech_signals_empty_text(self):
        self.assertEqual(outreach_agent.extract_tech_signals(""), [])
        self.assertEqual(outreach_agent.extract_tech_signals(None), [])


class LoadTargetsTests(unittest.TestCase):
    def test_drops_companies_without_named_contacts_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "targets.json"
            path.write_text(json.dumps([
                {"company": "Acme", "contacts": [{"name": "Jane", "channel": "email"}]},
                {"company": "Acme", "contacts": [{"name": "Duplicate"}]},
                {"company": "NoContacts", "contacts": []},
                {"company": "BadContact", "contacts": [{"title": "CTO"}]},
                {"company": "", "contacts": [{"name": "Ghost"}]},
            ]), encoding="utf-8")

            targets = outreach_agent.load_targets(path)

        self.assertEqual([t["company"] for t in targets], ["Acme"])
        self.assertEqual(len(targets[0]["contacts"]), 1)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(outreach_agent.load_targets(Path("/nonexistent/targets.json")), [])

    def test_non_list_json_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "targets.json"
            path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                outreach_agent.load_targets(path)

    def test_flat_shape_is_normalized_at_load_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "targets.json"
            path.write_text(json.dumps([
                {
                    "company": "DataPulse AI",
                    "website": "https://datapulse.example.com",
                    "contact_url": "https://www.linkedin.com/in/example-cto-datapulse",
                    "tech_stack": "Python, FastAPI, PostgreSQL, Docker, AWS",
                    "context": "Early-stage B2B data pipeline platform.",
                },
                {
                    "company": "LedgerFlow",
                    "website": "https://ledgerflow.example.com",
                    "contact_url": "https://x.com/example_founder",
                    "tech_stack": "Python, Django, PostgreSQL, Celery",
                    "context": "Automated financial reconciliation tools.",
                },
            ]), encoding="utf-8")

            targets = outreach_agent.load_targets(path)

        self.assertEqual([t["company"] for t in targets], ["DataPulse AI", "LedgerFlow"])

        datapulse = targets[0]
        self.assertEqual(datapulse["contacts"][0]["channel"], "linkedin")
        self.assertEqual(datapulse["contacts"][0]["title"], "CTO")
        self.assertEqual(
            datapulse["known_tech_stack"], ["Python", "FastAPI", "PostgreSQL", "Docker", "AWS"]
        )

        ledgerflow = targets[1]
        self.assertEqual(ledgerflow["contacts"][0]["channel"], "x")
        self.assertEqual(ledgerflow["contacts"][0]["title"], "Founder")


class NormalizeTargetRecordTests(unittest.TestCase):
    def test_passes_through_records_that_already_have_contacts(self):
        raw = {"company": "Acme", "contacts": [{"name": "Jane", "channel": "email"}]}
        self.assertIs(outreach_agent.normalize_target_record(raw), raw)

    def test_infers_contact_form_channel_for_unknown_hosts(self):
        raw = {"company": "Acme", "contact_url": "https://acme.example.com/team/jane"}
        normalized = outreach_agent.normalize_target_record(raw)
        self.assertEqual(normalized["contacts"][0]["channel"], "contact_form")
        self.assertEqual(normalized["contacts"][0]["name"], "Leadership Contact")

    def test_no_tech_stack_field_means_no_known_tech_stack_key(self):
        raw = {"company": "Acme", "contact_url": "https://acme.example.com"}
        normalized = outreach_agent.normalize_target_record(raw)
        self.assertNotIn("known_tech_stack", normalized)

    def test_infer_title_from_url_recognizes_role_hints(self):
        self.assertEqual(outreach_agent._infer_title_from_url("https://x.com/foo_cofounder"), "Co-Founder")
        self.assertEqual(outreach_agent._infer_title_from_url("https://linkedin.com/in/plain-jane"), "")

    def test_infer_channel_from_url(self):
        self.assertEqual(outreach_agent._infer_channel_from_url("https://www.linkedin.com/in/x"), "linkedin")
        self.assertEqual(outreach_agent._infer_channel_from_url("https://x.com/handle"), "x")
        self.assertEqual(outreach_agent._infer_channel_from_url("https://twitter.com/handle"), "x")
        self.assertEqual(outreach_agent._infer_channel_from_url("https://acme.com/contact"), "contact_form")


class BuildOutreachUrlTests(unittest.TestCase):
    def test_email_channel_prefills_gmail_compose_body(self):
        url, prefilled = outreach_agent.build_outreach_url(
            {"channel": "email", "email": "jane@acme.com"}, "hello there"
        )
        self.assertTrue(prefilled)
        self.assertIn("mail.google.com", url)
        self.assertIn("to=jane%40acme.com", url)
        self.assertIn("body=hello", url)

    def test_email_channel_requires_email(self):
        with self.assertRaises(ValueError):
            outreach_agent.build_outreach_url({"channel": "email"}, "hi")

    def test_linkedin_channel_returns_profile_url_not_prefilled(self):
        url, prefilled = outreach_agent.build_outreach_url(
            {"channel": "linkedin", "profile_url": "https://linkedin.com/in/jane"}, "hi"
        )
        self.assertEqual(url, "https://linkedin.com/in/jane")
        self.assertFalse(prefilled)

    def test_linkedin_channel_requires_profile_url(self):
        with self.assertRaises(ValueError):
            outreach_agent.build_outreach_url({"channel": "linkedin"}, "hi")

    def test_x_channel_returns_profile_url_not_prefilled(self):
        url, prefilled = outreach_agent.build_outreach_url(
            {"channel": "x", "profile_url": "https://x.com/example_founder"}, "hi"
        )
        self.assertEqual(url, "https://x.com/example_founder")
        self.assertFalse(prefilled)

    def test_x_channel_requires_profile_url(self):
        with self.assertRaises(ValueError):
            outreach_agent.build_outreach_url({"channel": "x"}, "hi")

    def test_contact_form_channel_falls_back_to_profile_url(self):
        url, prefilled = outreach_agent.build_outreach_url(
            {"channel": "contact_form", "profile_url": "https://acme.com/team/jane"}, "hi"
        )
        self.assertEqual(url, "https://acme.com/team/jane")
        self.assertFalse(prefilled)

    def test_contact_form_channel_requires_a_url(self):
        with self.assertRaises(ValueError):
            outreach_agent.build_outreach_url({"channel": "contact_form"}, "hi")


class DraftOutreachMessageTests(unittest.TestCase):
    def test_returns_none_without_gemini_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            result = outreach_agent.draft_outreach_message("Acme", {"name": "Jane"}, [], "context")
        self.assertIsNone(result)

    def test_accepts_optional_company_context(self):
        with patch.dict("os.environ", {}, clear=True):
            result = outreach_agent.draft_outreach_message(
                "Acme", {"name": "Jane"}, [], "context", "Series A fintech startup"
            )
        self.assertIsNone(result)


class SqliteTrackingTests(unittest.TestCase):
    def test_log_state_rejects_unknown_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            with self.assertRaises(ValueError):
                outreach_agent.log_state(db_path, "Acme", {"name": "Jane"}, "bogus")

    def test_log_state_round_trip_and_mark_sent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            contact = {"name": "Jane", "title": "CTO", "channel": "email"}

            outreach_agent.log_state(db_path, "Acme", contact, outreach_agent.STATE_DISCOVERED)
            outreach_agent.log_state(
                db_path, "Acme", contact, outreach_agent.STATE_DRAFTED, message="hello"
            )
            outreach_agent.log_state(
                db_path, "Acme", contact, outreach_agent.STATE_REVIEWED,
                url="https://mail.google.com/x", message="hello",
            )

            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT status FROM outreach_events ORDER BY id ASC"
            ).fetchall()
            conn.close()
            self.assertEqual(
                [r[0] for r in rows],
                [
                    outreach_agent.STATE_DISCOVERED,
                    outreach_agent.STATE_DRAFTED,
                    outreach_agent.STATE_REVIEWED,
                ],
            )

            marked = outreach_agent.mark_sent(db_path, "Acme", "Jane")
            self.assertTrue(marked)

            conn = sqlite3.connect(str(db_path))
            statuses = [
                r[0] for r in conn.execute(
                    "SELECT status FROM outreach_events ORDER BY id ASC"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(statuses[-1], outreach_agent.STATE_SENT)

    def test_mark_sent_returns_false_when_no_prior_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            self.assertFalse(outreach_agent.mark_sent(db_path, "Unknown Co", "Nobody"))


class PrepareOutreachTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_sends_and_records_full_state_sequence(self):
        """
        Integration-style: drives the real runner function with a mocked
        Playwright context/page and a mocked Gemini draft, and asserts that
        no click ever lands on a send-like control while every expected
        state transition is logged.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            target = {
                "company": "Acme",
                "website": "https://acme.example.com",
                "contacts": [
                    {"name": "Jane", "title": "CTO", "channel": "email", "email": "jane@acme.example.com"}
                ],
            }

            page = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            page.goto = AsyncMock()
            page.url = "https://mail.google.com/mail/?view=cm"
            page.title = AsyncMock(return_value="Acme")

            textarea = _mock_locator(count=1, visible=True, tag="textarea")
            textarea.fill = AsyncMock()
            send_button = _mock_locator(count=0, visible=False)
            send_button.click = AsyncMock()
            not_found = _mock_locator(count=0, visible=False)

            def locator_router(selector):
                if selector in outreach_agent.SEND_BUTTON_SELECTORS:
                    return send_button
                if selector in outreach_agent.AUTH_WALL_SELECTORS:
                    return not_found
                return textarea

            page.locator = MagicMock(side_effect=locator_router)

            context = AsyncMock()
            context.new_page = AsyncMock(return_value=page)

            semaphore = asyncio.Semaphore(2)
            rate_limiter = outreach_agent.RateLimiter(0.0)

            with patch.object(
                outreach_agent, "gather_tech_signals", new=AsyncMock(return_value=["Python"])
            ), patch.object(
                outreach_agent, "draft_outreach_message", return_value="a short technical hook"
            ):
                results = await outreach_agent.prepare_outreach_target(
                    context, target, semaphore, db_path, "candidate context", rate_limiter
                )

            self.assertEqual(len(results), 1)
            _, failure_label = results[0]
            self.assertIsNone(failure_label)

            send_button.click.assert_not_called()

            conn = sqlite3.connect(str(db_path))
            statuses = [
                r[0] for r in conn.execute(
                    "SELECT status FROM outreach_events ORDER BY id ASC"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(
                statuses,
                [
                    outreach_agent.STATE_DISCOVERED,
                    outreach_agent.STATE_DRAFTED,
                    outreach_agent.STATE_REVIEWED,
                ],
            )

    async def test_records_error_state_when_drafting_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            target = {
                "company": "Acme",
                "contacts": [{"name": "Jane", "channel": "email", "email": "jane@acme.example.com"}],
            }
            page = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            context = AsyncMock()
            context.new_page = AsyncMock(return_value=page)
            semaphore = asyncio.Semaphore(1)
            rate_limiter = outreach_agent.RateLimiter(0.0)

            with patch.object(
                outreach_agent, "gather_tech_signals", new=AsyncMock(return_value=[])
            ), patch.object(outreach_agent, "draft_outreach_message", return_value=None):
                results = await outreach_agent.prepare_outreach_target(
                    context, target, semaphore, db_path, "candidate context", rate_limiter
                )

            _, failure_label = results[0]
            self.assertIsNotNone(failure_label)

            conn = sqlite3.connect(str(db_path))
            statuses = [
                r[0] for r in conn.execute(
                    "SELECT status FROM outreach_events ORDER BY id ASC"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(
                statuses, [outreach_agent.STATE_DISCOVERED, outreach_agent.STATE_ERROR]
            )

    async def test_records_auth_required_and_skips_filling(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            target = {
                "company": "Acme",
                "contacts": [
                    {"name": "Jane", "title": "CTO", "channel": "linkedin",
                     "profile_url": "https://www.linkedin.com/in/jane"}
                ],
            }
            page = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            page.goto = AsyncMock()
            page.url = "https://www.linkedin.com/login?session_redirect=1"
            page.title = AsyncMock(return_value="Sign in to LinkedIn")

            context = AsyncMock()
            context.new_page = AsyncMock(return_value=page)
            semaphore = asyncio.Semaphore(1)
            rate_limiter = outreach_agent.RateLimiter(0.0)

            with patch.object(
                outreach_agent, "gather_tech_signals", new=AsyncMock(return_value=[])
            ), patch.object(
                outreach_agent, "draft_outreach_message", return_value="hi"
            ), patch.object(
                outreach_agent, "open_linkedin_message_composer", new=AsyncMock()
            ) as composer_mock, patch.object(
                outreach_agent, "fill_message_box", new=AsyncMock()
            ) as fill_mock:
                results = await outreach_agent.prepare_outreach_target(
                    context, target, semaphore, db_path, "candidate context", rate_limiter
                )

            _, failure_label = results[0]
            self.assertIsNotNone(failure_label)
            composer_mock.assert_not_called()
            fill_mock.assert_not_called()

            conn = sqlite3.connect(str(db_path))
            statuses = [
                r[0] for r in conn.execute(
                    "SELECT status FROM outreach_events ORDER BY id ASC"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(
                statuses,
                [outreach_agent.STATE_DISCOVERED, outreach_agent.STATE_DRAFTED, outreach_agent.STATE_AUTH_REQUIRED],
            )

    async def test_records_no_composer_found_when_field_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "outreach.db"
            target = {
                "company": "Acme",
                "contacts": [
                    {"name": "Jane", "title": "Founder", "channel": "x",
                     "profile_url": "https://x.com/jane"}
                ],
            }
            page = AsyncMock()
            page.is_closed = MagicMock(return_value=False)
            page.goto = AsyncMock()
            page.url = "https://x.com/jane"
            page.title = AsyncMock(return_value="Jane on X")
            page.locator = MagicMock(return_value=_mock_locator(count=0, visible=False))

            context = AsyncMock()
            context.new_page = AsyncMock(return_value=page)
            semaphore = asyncio.Semaphore(1)
            rate_limiter = outreach_agent.RateLimiter(0.0)

            with patch.object(
                outreach_agent, "gather_tech_signals", new=AsyncMock(return_value=[])
            ), patch.object(outreach_agent, "draft_outreach_message", return_value="hi"):
                results = await outreach_agent.prepare_outreach_target(
                    context, target, semaphore, db_path, "candidate context", rate_limiter
                )

            _, failure_label = results[0]
            self.assertIsNotNone(failure_label)

            conn = sqlite3.connect(str(db_path))
            statuses = [
                r[0] for r in conn.execute(
                    "SELECT status FROM outreach_events ORDER BY id ASC"
                ).fetchall()
            ]
            conn.close()
            self.assertEqual(
                statuses,
                [
                    outreach_agent.STATE_DISCOVERED, outreach_agent.STATE_DRAFTED,
                    outreach_agent.STATE_NO_COMPOSER_FOUND,
                ],
            )


if __name__ == "__main__":
    unittest.main()
