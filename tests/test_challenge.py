"""
Bot-challenge detection: blocking challenges pause the run, passive ones
(invisible reCAPTCHA badge, in-form checkbox) do not.

Pages are local set_content() markup whose iframes point at about:blank with
the real providers' URL shapes in the fragment, so nothing touches the network.
Skipped when Playwright's Chromium is not installed.
"""

import unittest

from playwright.async_api import async_playwright

import batch_runner
from bot_challenge import FORM_CAPTCHA_JS

INVISIBLE_BADGE = """
<div class="grecaptcha-badge" style="width:256px;height:60px;position:fixed;bottom:14px;right:0">
  <iframe title="reCAPTCHA" width="256" height="60"
          src="about:blank#recaptcha/api2/anchor?k=x&size=invisible"></iframe>
</div>"""
HIDDEN_PUZZLE = """
<div style="visibility:hidden;position:absolute;top:-10000px">
  <iframe title="recaptcha challenge expires in two minutes" width="400" height="580"
          src="about:blank#recaptcha/api2/bframe?k=x"></iframe>
</div>"""
VISIBLE_PUZZLE = """
<div style="position:absolute;top:20px;left:20px">
  <iframe title="recaptcha challenge expires in two minutes" width="400" height="580"
          src="about:blank#recaptcha/api2/bframe?k=x"></iframe>
</div>"""
CHECKBOX = """
<div class="g-recaptcha">
  <iframe title="reCAPTCHA" width="304" height="78"
          src="about:blank#recaptcha/api2/anchor?k=x&size=normal"></iframe>
</div>"""
FORM = "<form><label for='email'>Email</label><input id='email' type='email'></form>"


def page_html(body: str, title: str = "Apply") -> str:
    return f"<html><head><title>{title}</title></head><body>{FORM}{body}</body></html>"


class ChallengeDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception as error:
            await self.playwright.stop()
            self.skipTest(f"Chromium not available: {error}")
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def check(self, body: str, title: str = "Apply") -> tuple[str, str]:
        await self.page.set_content(page_html(body, title))
        blocking = await batch_runner.detect_challenge(self.page)
        widget = await self.page.evaluate(FORM_CAPTCHA_JS)
        return blocking, widget

    async def test_invisible_badge_is_not_a_challenge(self):
        # The false positive behind every "Bot challenge detected" in the logs.
        self.assertEqual(await self.check(INVISIBLE_BADGE + HIDDEN_PUZZLE), ("", ""))

    async def test_visible_image_puzzle_blocks(self):
        blocking, _ = await self.check(VISIBLE_PUZZLE)
        self.assertEqual(blocking, "reCAPTCHA puzzle")

    async def test_cloudflare_interstitial_blocks(self):
        blocking, _ = await self.check("", title="Just a moment...")
        self.assertIn("Just a moment", blocking)

    async def test_checkbox_in_form_is_reported_not_blocking(self):
        blocking, widget = await self.check(CHECKBOX)
        self.assertEqual(blocking, "")
        self.assertIn("I'm not a robot", widget)

    async def test_waits_for_human_then_continues(self):
        await self.page.set_content(page_html(
            "<script>setTimeout(() => { document.title = 'Apply'; }, 1000);</script>",
            title="Just a moment...",
        ))
        self.assertTrue(await batch_runner.wait_for_challenge_clear(self.page, "Job", timeout_s=15))

    async def test_gives_up_after_timeout(self):
        await self.page.set_content(page_html("", title="Just a moment..."))
        self.assertFalse(await batch_runner.wait_for_challenge_clear(self.page, "Job", timeout_s=0.5))


if __name__ == "__main__":
    unittest.main()
