"""Concurrent, review-gated job application batch runner.

Opens queued jobs in tabs within one headed Playwright browser, fills known
fields concurrently, and leaves every prepared tab open for manual review.
This module never clicks a submit button.
"""

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright

from candidate_answers import draft_answers, is_eligible_open_question
from application_history import filter_unhandled
from bot_challenge import BLOCKING_CHALLENGE_JS, FORM_CAPTCHA_JS

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_QUEUE_PATH = BASE_DIR / "queues" / "pending_jobs.json"
APPLIED_LOG_PATH = BASE_DIR / "queues" / "applied_log.json"
SELF_HEAL_LOG_PATH = BASE_DIR / "self_heal_log.json"
FAILURE_DIR = BASE_DIR / "failures"
PROFILE_PATH = BASE_DIR / "profile.json"
SELF_HEAL_MODEL = "gemini-2.5-flash"

FIELD_SELECTORS = {
    "full_name": [
        "input[name='_systemfield_name']",
        "input[aria-label='Legal Name' i]",
        "input[name='name']",
    ],
    "first_name": [
        "input[name='_systemfield_name_first']",
        "input#first-name-input",
        "input#first_name",
        "input[name='job_application[first_name]']",
        "input[data-automation-id='legalNameSection_firstName']",
        "input[aria-label='First Name' i]",
        "input[placeholder='First Name' i]",
        "input[id*='first' i][id*='name' i]",
        "input[name*='first' i]",
    ],
    "last_name": [
        "input[name='_systemfield_name_last']",
        "input#last-name-input",
        "input#last_name",
        "input[name='job_application[last_name]']",
        "input[data-automation-id='legalNameSection_lastName']",
        "input[aria-label='Last Name' i]",
        "input[placeholder='Last Name' i]",
        "input[id*='last' i][id*='name' i]",
        "input[name*='last' i]",
    ],
    "email": [
        "input[name='_systemfield_email']",
        "input#email-input",
        "input#email",
        "input[name='job_application[email]']",
        "input[data-automation-id='email']",
        "input[type='email']",
        "input[aria-label='Email' i]",
        "input[placeholder='Email' i]",
        "input[name*='email' i]",
    ],
    "phone": [
        "input[name='_systemfield_phone']",
        "input#phone-input",
        "input#phone",
        "input[name='job_application[phone]']",
        "input[data-automation-id='phone-number']",
        "input[type='tel']",
        "input[aria-label='Phone' i]",
        "input[placeholder='Phone' i]",
        "input[name*='phone' i]",
    ],
    "preferred_name": [
        "input#preferred_name",
        "input[aria-label='Preferred First Name' i]",
        "input[name*='preferred_name' i]",
    ],
    "linkedin_url": [
        "input[aria-label='LinkedIn Profile' i]",
        "input[name*='linkedin' i]",
        "input[id*='linkedin' i]",
    ],
    "portfolio_url": [
        "input[aria-label*='Portfolio' i]",
        "input[aria-label*='Website' i]",
        "input[name*='portfolio' i]",
        "input[name*='website' i]",
        "input[id*='portfolio' i]",
        "input[id*='website' i]",
    ],
    "github_url": [
        "input[aria-label*='GitHub' i]",
        "input[name*='github' i]",
        "input[id*='github' i]",
    ],
    "location": [
        "input[name='_systemfield_location']",
        "input[aria-label*='Location' i]",
        "input[placeholder*='Start typing' i]",
        "input[id*='location' i]",
        "input[name*='location' i]",
        "input[name*='city' i]",
    ],
    "resume": [
        "input[type='file'][name*='resume' i]",
        "input[type='file'][id*='resume' i]",
        "input[data-automation-id='file-upload-input-ref']",
        "input[type='file']",
    ],
}

# How long a tab waits for the human to clear a blocking challenge before the
# job is given up. Set from --challenge-timeout.
CHALLENGE_TIMEOUT_SECONDS = 300

_json_lock = asyncio.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(value: str) -> str:
    return value.strip().rstrip("~")


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def load_queue(path: Path, ignore_history: bool = False) -> list[dict]:
    if not Path(path).exists():
        raise FileNotFoundError(
            f"Queue file not found: {path}. Run `python scraper.py all` to create one."
        )
    raw_jobs = load_json(path, [])
    if not isinstance(raw_jobs, list):
        raise ValueError(f"Queue must contain a JSON list: {path}")

    jobs: list[dict] = []
    seen: set[str] = set()
    for item in raw_jobs:
        job = dict(item) if isinstance(item, dict) else {"url": item}
        raw_url = job.get("url")
        if not isinstance(raw_url, str):
            continue
        url = normalize_url(raw_url)
        if not url or url in seen:
            continue
        job["url"] = url
        jobs.append(job)
        seen.add(url)
    if ignore_history:
        print(f"  [dedup] Bypassed - re-preparing all {len(jobs)} queued job(s).")
        return jobs
    return filter_unhandled(jobs)


async def append_json_record(path: Path, record: dict) -> None:
    async with _json_lock:
        records = await asyncio.to_thread(load_json, path, [])
        if not isinstance(records, list):
            records = []
        records.append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            path.write_text, json.dumps(records, indent=2), encoding="utf-8"
        )


async def log_status(job: dict, status: str, error: str = "") -> None:
    await append_json_record(
        APPLIED_LOG_PATH,
        {
            "timestamp": utc_now(),
            "url": job.get("url", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "status": status,
            "error": error,
        },
    )


async def first_visible(page: Page, selectors: list[str]) -> Locator | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception:
            continue
    return None


async def fill_field(page: Page, field_name: str, value: str) -> bool:
    if not value:
        return False
    locator = await first_visible(page, FIELD_SELECTORS[field_name])
    if locator is None:
        raise RuntimeError(f"No visible selector found for '{field_name}'")
    await locator.fill(str(value))
    print(f"  [ok] Filled '{field_name}'")
    return True


async def diagnose_failure(page: Page, action_name: str, error: Exception, url: str) -> str:
    FAILURE_DIR.mkdir(parents=True, exist_ok=True)
    safe_action = re.sub(r"[^a-zA-Z0-9_-]", "_", action_name)[:60]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    screenshot_path = FAILURE_DIR / f"{safe_action}_{stamp}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        screenshot_path = None

    try:
        page_text = await page.locator("body").inner_text(timeout=3000)
        page_text = page_text[:3000]
    except Exception:
        page_text = "(could not read page text)"

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        suggestion = "Gemini diagnosis unavailable: GEMINI_API_KEY is not set in this shell."
    else:
        prompt = (
            "Diagnose this failed job-form automation step. Return only a short, "
            "plain-language suggestion for a human reviewer; never return executable code.\n\n"
            f"Goal: {action_name}\nError: {error}\nPage text:\n{page_text}"
        )
        try:
            from google import genai

            def generate() -> str:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(model=SELF_HEAL_MODEL, contents=prompt)
                return (response.text or "").strip()

            suggestion = await asyncio.to_thread(generate)
        except Exception as ai_error:
            suggestion = f"Gemini diagnosis unavailable: {ai_error}"

    await append_json_record(
        SELF_HEAL_LOG_PATH,
        {
            "timestamp": utc_now(),
            "action": action_name,
            "url": url,
            "error": str(error),
            "gemini_suggestion": suggestion,
            "screenshot": str(screenshot_path.relative_to(BASE_DIR)) if screenshot_path else "",
        },
    )
    print(f"  [self-heal] {action_name}: {suggestion}")
    return suggestion


class PermanentFailure(Exception):
    """
    A failure retrying cannot fix - a pulled posting, a closed req.

    self_heal_action re-raises these immediately instead of burning retries
    and a Gemini diagnosis call on a page that will never change.
    """


async def self_heal_action(
    page: Page,
    action_name: str,
    action: Callable[[], Awaitable[Any]],
    url: str,
    max_retries: int = 3,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [self-heal] Attempt {attempt}/{max_retries}: {action_name}")
            return await action()
        except PermanentFailure:
            # Nothing to heal: surface it immediately.
            raise
        except Exception as error:
            last_error = error
            print(f"  [self-heal] '{action_name}' failed: {error}")
            await diagnose_failure(page, action_name, error, url)
            if attempt < max_retries:
                await page.wait_for_timeout(1000 * attempt)
    raise RuntimeError(f"{action_name} failed after {max_retries} attempts: {last_error}")


async def detect_challenge(page: Page) -> str:
    """Return why the page is blocked by a bot challenge, or "" if it is not."""
    try:
        return await page.evaluate(BLOCKING_CHALLENGE_JS) or ""
    except Exception:
        # Mid-navigation (e.g. Cloudflare redirecting after a pass).
        return ""


async def wait_for_challenge_clear(page: Page, title: str, timeout_s: float | None = None) -> bool:
    """
    If a blocking challenge is showing, bring the tab forward and wait for the
    human to solve it. Returns True once the page is clear (or never was
    blocked), False if the timeout ran out. Never interacts with the challenge.
    """
    reason = await detect_challenge(page)
    if not reason:
        return True
    timeout_s = CHALLENGE_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    try:
        await page.bring_to_front()
    except Exception:
        pass
    print(f"  [action] {reason} in tab '{title}'. Please solve it in the browser; "
          f"filling continues automatically (waiting up to {int(timeout_s)}s).")
    deadline = asyncio.get_running_loop().time() + timeout_s
    next_reminder = asyncio.get_running_loop().time() + 60
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(2)
        if not await detect_challenge(page):
            print(f"  [ok] Challenge cleared in '{title}'; continuing.")
            # Let the real page load after the challenge redirects.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            return True
        if asyncio.get_running_loop().time() >= next_reminder:
            print(f"  [action] Still waiting on the challenge in '{title}'...")
            next_reminder += 60
    return False


async def report_form_captcha(page: Page) -> str:
    """Remind the human about a CAPTCHA widget they must tick before submitting."""
    try:
        widget = await page.evaluate(FORM_CAPTCHA_JS) or ""
    except Exception:
        return ""
    if widget:
        print(f"  [review] This form has a {widget}; tick it yourself before submitting.")
    return widget


# Apply-link wording varies by ATS and by companies that skin their own
# careers site on top of one. Stripe uses "Apply now"/"Apply for this role";
# stock Greenhouse uses "Apply for this Job".
# NOTE: Playwright matches a regex against the raw accessible name, which is
# NOT whitespace-normalized and can carry invisible formatting characters.
# Stripe's links compute to "Apply now \u2060" (U+2060 WORD JOINER), and JS
# \s does not match U+2060 - so a plain ^...$ anchor silently matches nothing.
# _PAD covers whitespace plus the zero-width/invisible range U+200B-U+206F.
_PAD = r"[\s\u200b-\u206f]*"
APPLY_LINK_RE = re.compile(
    r"^" + _PAD + r"(apply now|apply for this (job|role|position)|apply)" + _PAD + r"$",
    re.IGNORECASE,
)

# Companies like Stripe render the real form inside an ATS iframe on their own
# domain, so page-level locators find nothing. Navigating straight to the embed
# URL promotes the form to the top-level document and every selector works.
EMBEDDED_FORM_HOSTS = (
    "job-boards.greenhouse.io/embed",
    "boards.greenhouse.io/embed",
    "jobs.lever.co",
    "jobs.ashbyhq.com/embed",
)


def find_embedded_form_url(page) -> str | None:
    """Return the URL of an ATS form iframe on this page, if there is one."""
    for frame in page.frames:
        url = frame.url or ""
        if any(host in url for host in EMBEDDED_FORM_HOSTS):
            return url
    return None


# Apply controls are not always links (Samsara renders a <button>) and their
# accessible name often carries the role title too ("Apply Now\nfor Agentic AI
# Engineer" on Elastic), so match on a prefix rather than the whole name.
APPLY_TEXT_RE = re.compile(r"^[\s\u200b-\u206f]*apply\b", re.IGNORECASE)
# "Apply filters" on a search UI is not an application control.
APPLY_REJECT_RE = re.compile(r"\bfilters?\b", re.IGNORECASE)
# A posting pulled between the scrape and the run - fail fast instead of
# burning three self-heal attempts on a dead page.
DEAD_POSTING_RE = re.compile(
    r"(page not found|no longer accepting|position (has been|is) closed|"
    r"job (is )?no longer available)", re.IGNORECASE
)


async def _page_is_dead_posting(page: Page) -> bool:
    try:
        body = await page.locator("body").inner_text()
    except Exception:
        return False
    return bool(DEAD_POSTING_RE.search(body or ""))


async def _click_apply_control(page: Page) -> bool:
    """Click the first genuine apply link or button. Returns True if one was clicked."""
    for role in ("link", "button"):
        controls = page.get_by_role(role, name=APPLY_TEXT_RE)
        for index in range(min(await controls.count(), 10)):
            control = controls.nth(index)
            if not await control.is_visible():
                continue
            try:
                text = (await control.inner_text()) or ""
            except Exception:
                text = ""
            if APPLY_REJECT_RE.search(text):
                continue
            if role == "link":
                href = (await control.get_attribute("href")) or ""
                # Decoy links that just re-anchor the SPA (e.g. ".../#/").
                if not href or href.rstrip("/").endswith("#"):
                    continue
            await control.click()
            await page.wait_for_load_state("domcontentloaded")
            return True
    return False


APPLICATION_TAB_RE = re.compile(r"^Application$", re.IGNORECASE)


async def _wait_for_form_signal(page: Page, timeout_ms: int = 8000) -> None:
    """
    Wait until the page shows a form, an ATS iframe, or a way to reach one.

    Ashby SPAs and embedded Greenhouse iframes render after domcontentloaded,
    so checking immediately found nothing and the first attempt always failed
    (Brex, OpenAI); only self-heal's pause before the retry made it work.
    """
    for _ in range(max(1, timeout_ms // 500)):
        if (
            await first_visible(page, FIELD_SELECTORS["email"])
            or find_embedded_form_url(page)
            or await page.get_by_role("tab", name=APPLICATION_TAB_RE).count()
            or await page.get_by_role("link", name=APPLY_TEXT_RE).count()
            or await page.get_by_role("button", name=APPLY_TEXT_RE).count()
        ):
            return
        await page.wait_for_timeout(500)


async def _wait_for_hydration(page: Page, timeout_ms: int = 15000) -> None:
    """
    Let a server-rendered form finish loading its JavaScript before filling.

    Greenhouse's job-boards pages render the inputs server-side, so they are
    visible before React is attached. A résumé set in that window never
    uploaded (the Attach widget kept saying "Attach" and the required Resume/CV
    stayed empty), and typed contact fields were wiped when React took over.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


async def open_application_form(page: Page) -> None:
    await _open_application_form(page)
    await _wait_for_hydration(page)


async def _open_application_form(page: Page) -> None:
    await _wait_for_form_signal(page)
    if await first_visible(page, FIELD_SELECTORS["email"]):
        return

    # The form may already be embedded in an iframe on the landing page.
    embed_url = find_embedded_form_url(page)
    if embed_url:
        await page.goto(embed_url, wait_until="domcontentloaded")
        print("  [ok] Opened embedded application form")
        return

    if await _page_is_dead_posting(page):
        raise PermanentFailure("Posting is no longer available (page not found/closed)")

    if await _click_apply_control(page):
        # The apply page itself often hosts the ATS iframe rather than the form.
        await page.wait_for_timeout(1500)
        embed_url = find_embedded_form_url(page)
        if embed_url:
            await page.goto(embed_url, wait_until="domcontentloaded")
            print("  [ok] Opened embedded application form")
            return
        if await first_visible(page, FIELD_SELECTORS["email"]):
            print("  [ok] Opened application form")
            return

    application_tab = page.get_by_role("tab", name=APPLICATION_TAB_RE).first
    await application_tab.click(timeout=5000)
    # The tab's fields render after the click; wait so contact filling
    # doesn't run against an empty panel.
    try:
        await page.locator(", ".join(FIELD_SELECTORS["email"])).first.wait_for(
            state="visible", timeout=8000
        )
    except Exception:
        pass
    print("  [ok] Opened application tab")


async def fill_contact_fields(page: Page, profile: dict) -> None:
    full_name = " ".join(
        part for part in (profile.get("first_name", ""), profile.get("last_name", "")) if part
    )
    full_name_input = await first_visible(page, FIELD_SELECTORS["full_name"])
    if full_name_input is not None:
        await full_name_input.fill(full_name)
        print("  [ok] Filled 'full_name'")
    else:
        await fill_field(page, "first_name", profile.get("first_name", ""))
        await fill_field(page, "last_name", profile.get("last_name", ""))
    await fill_field(page, "email", profile.get("email", ""))
    phone = profile.get("phone", "")
    if phone and await first_visible(page, FIELD_SELECTORS["phone"]):
        await fill_field(page, "phone", phone)


async def upload_resume(page: Page, profile: dict) -> None:
    configured_path = profile.get("resume_path", "")
    resume_path = Path(configured_path)
    if not resume_path.is_absolute():
        resume_path = (BASE_DIR / resume_path).resolve()
    if not resume_path.is_file():
        raise FileNotFoundError(f"Resume not found: {resume_path}")

    locator = None
    for selector in FIELD_SELECTORS["resume"]:
        candidate = page.locator(selector).first
        if await candidate.count():
            locator = candidate
            break
    if locator is None:
        raise RuntimeError("No resume upload input found")

    # Holding the file in the <input> is not the same as the site accepting it:
    # Greenhouse ignored files set before its JavaScript loaded. The sites we
    # support show the file name once the upload registers, so wait for that.
    stem = resume_path.stem
    for attempt in range(2):
        await locator.set_input_files(str(resume_path))
        for _ in range(10):
            await page.wait_for_timeout(500)
            try:
                if stem in await page.locator("body").inner_text():
                    print(f"  [ok] Uploaded resume: {resume_path.name} (confirmed on page)")
                    return
            except Exception:
                pass
        if attempt == 0:
            await _wait_for_hydration(page)
    print(f"  [review] Resume set but the page never showed '{resume_path.name}'. "
          "CHECK THE RÉSUMÉ IS ATTACHED before submitting.")


async def select_combobox_answer(page: Page, question: str, answer: str) -> bool:
    locator = page.get_by_role("combobox", name=re.compile(question, re.IGNORECASE)).first
    if not await locator.count() or not await locator.is_visible():
        return False
    # A native <select> also has the combobox role but cannot be typed into;
    # fill() on it raised and aborted the whole application.
    if await locator.evaluate("e => e.tagName") == "SELECT":
        answer_re = re.compile(rf"^\s*{re.escape(answer)}(?:\s|$)", re.IGNORECASE)
        labels = await locator.locator("option").all_inner_texts()
        match = next((label for label in labels if answer_re.search(label)), None)
        if match is None:
            print(f"  [review] No '{answer}' option for '{question}'; left for you")
            return False
        await locator.select_option(label=match)
        print(f"  [ok] Answered '{question}'")
        return True
    await page.keyboard.press("Escape")
    await locator.click()
    await locator.fill(answer)
    option = page.get_by_role(
        "option", name=re.compile(rf"^{re.escape(answer)}(?:\s|$)", re.IGNORECASE)
    ).last
    try:
        # 3s was not enough for a slow react-select menu, and a timeout here
        # RAISED - which aborted the whole application, not just this question.
        # Twilio was lost that way on a run where the same form had filled fine
        # minutes earlier: contact fields and resume went in, then the job died
        # before work authorisation, EEO, the consent box and the review gate.
        # An option that never appears is a question left unanswered, which the
        # required-field validator already reports; it is not a reason to stop.
        await option.wait_for(state="visible", timeout=9000)
    except Exception:
        await page.keyboard.press("Escape")
        print(f"  [review] Could not open options for '{question}'; left for you")
        return False
    await option.click()
    print(f"  [ok] Answered '{question}'")
    return True


async def fill_known_profile_questions(page: Page, profile: dict) -> None:
    for field_name in ("preferred_name", "linkedin_url", "portfolio_url", "github_url"):
        value = profile.get(field_name, "")
        if value and await first_visible(page, FIELD_SELECTORS[field_name]):
            await fill_field(page, field_name, value)

    years = profile.get("years_professional_software_engineering")
    if years is not None:
        await select_combobox_answer(
            page,
            r"over 3 years.*professional software engineering",
            "Yes" if years > 3 else "No",
        )

    for profile_key, question in (
        ("ruby_on_rails_rating", r"rate yourself in Ruby on Rails"),
        ("python_rating", r"rate yourself in Python"),
    ):
        value = profile.get(profile_key)
        if value is not None:
            answer = "0 (no experience)" if value == 0 else str(value)
            await select_combobox_answer(page, question, answer)

    country = profile.get("country")
    if country:
        await select_combobox_answer(page, r"currently live in this location", "Yes")
        await select_combobox_answer(page, r"current country of residence", country)
        if NORTH_AMERICA_COUNTRY_RE.search(country):
            await select_combobox_answer(page, r"located in the US or Canada", "Yes")

    restrictions = profile.get("employment_restrictions")
    if restrictions is not None:
        await select_combobox_answer(
            page,
            r"employment agreements.*post-employment restrictions",
            "Yes" if restrictions else "No",
        )

    if "requires_sponsorship" in profile:
        await select_combobox_answer(
            page,
            r"require sponsorship.*visa",
            "Yes" if profile["requires_sponsorship"] else "No",
        )

    previous_gitlab = profile.get("previously_worked_at_gitlab")
    if previous_gitlab is not None:
        await select_combobox_answer(
            page,
            r"previously worked at or consulted for GitLab",
            "Yes" if previous_gitlab else "No",
        )


async def fill_open_ended_fields(page: Page) -> None:
    """Draft and fill eligible blank prose fields for human review."""
    candidates: list[tuple[Locator, str]] = []
    fields = page.locator("textarea, input[type='text']")
    for index in range(min(await fields.count(), 150)):
        field = fields.nth(index)
        try:
            if not await field.is_visible() or (await field.input_value()).strip():
                continue
            if ((await field.get_attribute("role")) or "").lower() == "combobox":
                continue
            label = await field.evaluate(
                """element => {
                    const labels = element.labels ? Array.from(element.labels) : [];
                    return labels.map(label => label.innerText.trim()).filter(Boolean).join(' | ')
                        || element.getAttribute('aria-label')
                        || element.getAttribute('placeholder')
                        || '';
                }"""
            )
            tag_name = await field.evaluate("element => element.tagName.toLowerCase()")
            input_type = (await field.get_attribute("type")) or tag_name
            if is_eligible_open_question(label, tag_name, input_type):
                candidates.append((field, label))
        except Exception:
            continue

    if not candidates:
        return
    try:
        page_context = f"{await page.title()}\n{(await page.locator('body').inner_text())[:4000]}"
    except Exception:
        page_context = ""
    answers = await asyncio.to_thread(
        draft_answers, [label for _, label in candidates], page_context
    )
    for (field, label), answer in zip(candidates, answers):
        if not answer:
            print(f"  [review] No grounded draft for: {label}")
            continue
        await field.fill(answer)
        print(f"  [ok] Drafted response for: {label}")



# ---------------------------------------------------------------------------
# Checkbox and multi-step form handling
# ---------------------------------------------------------------------------
# Only checkboxes the form itself marks as required are auto-checked, and even
# then two categories are always left for the human:
#
#   SENSITIVE  - voluntary self-identification (EEO/demographics) and marketing
#                consent. These are personal disclosures; an agent must not
#                answer them on someone's behalf.
#   ATTESTATION - statements of fact the applicant personally certifies. Ticking
#                these automatically would assert something unread.
#
# Everything skipped is logged so it is visible at the review gate.
SENSITIVE_CHECKBOX_KEYWORDS = [
    "gender", "race", "ethnic", "veteran", "disability", "self-identif",
    "self identif", "demographic", "lgbt", "sexual orientation", "pronoun",
    "marketing", "newsletter", "subscribe", "promotional", "text message",
    "sms", "contact me about", "future opportunities",
]
ATTESTATION_CHECKBOX_KEYWORDS = [
    "certify", "attest", "under penalty", "u.s. person", "us person",
    "export control", "citizen", "i confirm that", "i declare",
]

# Buttons that advance a multi-step form. These are DETECTED and reported but
# never clicked: on several ATS platforms the final step's control is labelled
# "Continue" or "Review" and posts the application. Not clicking them is what
# keeps the "this agent never submits" guarantee true.
PAGINATION_BUTTON_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Review')",
    "input[type='button'][value='Next']",
    "input[type='button'][value='Continue']",
]


def _classify_checkbox(label_text: str) -> str:
    """Return 'sensitive', 'attestation', or 'ok' for a checkbox's label text."""
    text = (label_text or "").lower()
    if any(k in text for k in SENSITIVE_CHECKBOX_KEYWORDS):
        return "sensitive"
    if any(k in text for k in ATTESTATION_CHECKBOX_KEYWORDS):
        return "attestation"
    return "ok"


def _summarize(text: str, limit: int = 60) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + ("..." if len(flat) > limit else "")



async def _checkbox_group_key(checkbox: Locator) -> str:
    """Identify the question group a checkbox belongs to (name attr, else fieldset)."""
    name = await checkbox.get_attribute("name")
    if name:
        return f"name:{name}"
    legend = await checkbox.evaluate(
        "e => { const f = e.closest('fieldset,[role=group]');"
        " return f ? (f.querySelector('legend')?.textContent || 'group').trim() : ''; }"
    )
    return f"group:{legend}" if legend else ""


async def _checkbox_is_required(checkbox: Locator) -> bool:
    """True only if the markup itself marks this checkbox as required."""
    if await checkbox.get_attribute("required") is not None:
        return True
    if (await checkbox.get_attribute("aria-required") or "").lower() == "true":
        return True
    return False


async def _checkbox_label_text(page: Page, checkbox: Locator) -> str:
    """Best-effort label text for a checkbox, from aria-label, <label for>, or parent."""
    aria = await checkbox.get_attribute("aria-label")
    if aria:
        return aria
    cb_id = await checkbox.get_attribute("id")
    if cb_id:
        label = page.locator(f"label[for='{cb_id}']").first
        if await label.count():
            try:
                return await label.inner_text()
            except Exception:
                pass
    try:
        return await checkbox.locator("xpath=..").inner_text()
    except Exception:
        return ""


async def check_required_checkboxes(page: Page) -> int:
    """
    Tick only checkboxes the form marks required, excluding self-identification
    and personal attestations. Returns the number actually checked.

    Errors are allowed to propagate so self_heal_action can see them; only the
    per-checkbox label lookup is tolerant, since a missing label is not a failure.
    """
    checked = 0
    checkboxes = page.locator("input[type='checkbox']")
    total = min(await checkboxes.count(), 100)

    # Count group membership first so multi-selects can be recognised.
    group_sizes: dict[str, int] = {}
    for index in range(total):
        box = checkboxes.nth(index)
        if not await box.is_visible():
            continue
        key = await _checkbox_group_key(box)
        if key:
            group_sizes[key] = group_sizes.get(key, 0) + 1

    # Groups that already have a selection were answered elsewhere (e.g.
    # select_work_countries) and must not be re-reported as outstanding.
    satisfied_groups: set[str] = set()
    for index in range(total):
        box = checkboxes.nth(index)
        if not await box.is_visible() or not await box.is_checked():
            continue
        key = await _checkbox_group_key(box)
        if key:
            satisfied_groups.add(key)

    reported_groups: set[str] = set()
    for index in range(total):
        box = checkboxes.nth(index)
        if not await box.is_visible():
            continue
        if await box.is_checked():
            continue
        if not await _checkbox_is_required(box):
            continue

        key = await _checkbox_group_key(box)
        if key and group_sizes.get(key, 1) > 1:
            if key in satisfied_groups:
                continue  # an earlier step already answered this question
            if key not in reported_groups:
                reported_groups.add(key)
                print(
                    f"  [review] Required multi-select left for you "
                    f"({group_sizes[key]} options): {_summarize(key)}"
                )
            continue

        label = await _checkbox_label_text(page, box)
        verdict = _classify_checkbox(label)
        if verdict != "ok":
            print(f"  [review] Required checkbox left for you ({verdict}): {_summarize(label)}")
            continue

        await box.check()
        checked += 1
        print(f"  [ok] Checked required box: {_summarize(label)}")

    if checked:
        print(f"  [ok] Checked {checked} required checkbox(es)")
    return checked


# Agreements the applicant has opted (profile["auto_accept_legal_acknowledgements"])
# to have ticked for them: "I have read the Arbitration Agreement", "I certify
# my answers are true". Boxes that certify a FACT about the person - citizenship,
# U.S.-person/export-control status - are never auto-ticked.
LEGAL_ACK_RE = re.compile(
    r"acknowledge|arbitration|i\s+confirm|have\s+read|certify|i\s+agree|"
    r"terms\s+(and|&)\s+conditions",
    re.IGNORECASE,
)
FACTUAL_ATTESTATION_RE = re.compile(
    r"citizen|u\.?s\.?\s+person|export\s+control|under\s+penalty|green\s+card|permanent\s+resident",
    re.IGNORECASE,
)


def is_auto_acceptable_acknowledgement(label: str) -> bool:
    if _classify_checkbox(label) == "sensitive":
        return False
    return bool(LEGAL_ACK_RE.search(label or "")) and not FACTUAL_ATTESTATION_RE.search(label or "")


async def accept_legal_acknowledgements(page: Page, profile: dict) -> int:
    """Tick agreement/acknowledgement checkboxes, only if the profile opts in."""
    if not profile.get("auto_accept_legal_acknowledgements"):
        return 0
    checked = 0
    boxes = page.locator("input[type='checkbox']")
    total = min(await boxes.count(), 100)
    group_sizes: dict[str, int] = {}
    for index in range(total):
        key = await _checkbox_group_key(boxes.nth(index))
        if key:
            group_sizes[key] = group_sizes.get(key, 0) + 1
    for index in range(total):
        box = boxes.nth(index)
        if not await box.is_visible() or await box.is_checked():
            continue
        key = await _checkbox_group_key(box)
        if key and group_sizes.get(key, 1) > 1:
            continue  # a multi-select question, not a single agreement box
        label = await _checkbox_label_text(page, box)
        if not is_auto_acceptable_acknowledgement(label):
            continue
        await box.check()
        checked += 1
        print(f"  [ok] Accepted acknowledgement: {_summarize(label)}")
    return checked


async def report_multi_step_form(page: Page) -> bool:
    """Detect (never click) a pagination control and report it at the review gate."""
    for selector in PAGINATION_BUTTON_SELECTORS:
        button = page.locator(selector).first
        if await button.count() and await button.first.is_visible():
            try:
                label = _summarize(await button.inner_text(), 24)
            except Exception:
                label = selector
            print(f"  [review] Multi-step form: a '{label}' step follows - not clicked.")
            return True
    return False


FIELD_LABEL_JS = r"""e => {
  const al = e.getAttribute('aria-label');
  if (al) return al.trim();
  if (e.id) {
    const l = document.querySelector('label[for="' + e.id + '"]');
    if (l && l.innerText) return l.innerText.trim();
  }
  return '';
}"""


def is_location_label(label: str) -> bool:
    """
    True if a field's label reads like a plain location field.

    The location selectors match on "location" anywhere in the aria-label, so
    Brex's free-text "If you're not authorized to work at the stated location,
    what sponsorship would you require?" was matched and got the city typed
    into it. A real location label is short and is not a sponsorship or
    work-authorization question. Unlabelled fields (matched by id/name) pass.
    """
    label = (label or "").strip()
    if not label:
        return True
    if len(label) > 60:
        return False
    return not (SPONSORSHIP_QUESTION_RE.search(label) or AUTHORIZATION_QUESTION_RE.search(label))


async def find_location_field(page: Page) -> Locator | None:
    for selector in FIELD_SELECTORS["location"]:
        candidates = page.locator(selector)
        for index in range(min(await candidates.count(), 10)):
            candidate = candidates.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                label = await candidate.evaluate(FIELD_LABEL_JS)
            except Exception:
                continue
            if is_location_label(label):
                return candidate
    # Custom questions like OpenAI's "Where are you currently located?" are
    # type-ahead comboboxes with no "location" in any attribute.
    boxes = page.locator("input[role='combobox']")
    for index in range(min(await boxes.count(), 40)):
        box = boxes.nth(index)
        try:
            if not await box.is_visible():
                continue
            label = await box.evaluate(COMBOBOX_LABEL_JS)
        except Exception:
            continue
        if LOCATION_QUESTION_RE.search(label or "") and is_location_label(label):
            return box
    return None


LOCATION_QUESTION_RE = re.compile(
    r"where\s+are\s+you\s+(currently\s+)?(located|based)|current\s+location", re.IGNORECASE
)


async def fill_location_field(page: Page, profile: dict) -> bool:
    """
    Fill a location/city field from profile['location_city'].

    Type-ahead widgets (react-select on Greenhouse) render their menu
    asynchronously - roughly 1.2s on Reddit's form - and their options must be
    scoped to THIS widget via aria-controls. Clicking a page-wide "first
    option" can land in an unrelated dropdown, and pressing ArrowDown/Enter
    before the menu opens clears the input outright. So: wait for this
    widget's own listbox, pick the option that matches, and if no menu ever
    appears leave the typed text alone rather than blind-keying it away.
    """
    city = profile.get("location_city", "")
    if not city:
        return False
    field = await find_location_field(page)
    if field is None:
        return False

    # The menu is the flaky half of this widget: an embedded form under load can
    # overrun a 5s wait, and a miss leaves the field empty rather than merely
    # unconfirmed. Two attempts with a longer wait; warn only if both fail.
    for _attempt in range(2):
        await field.click()
        await field.fill(city)

        listbox_id = await field.get_attribute("aria-controls")
        options = (
            page.locator(f"[id='{listbox_id}'] [role='option']")
            if listbox_id
            else page.get_by_role("option")
        )
        try:
            await options.first.wait_for(state="visible", timeout=9000)
        except Exception:
            options = None

        if options is not None and await options.count():
            # Prefer an option that actually matches the city, not just the first.
            head = city.split(",")[0].strip().lower()
            chosen = options.first
            for index in range(min(await options.count(), 10)):
                candidate = options.nth(index)
                try:
                    text = (await candidate.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if text.startswith(head):
                    chosen = candidate
                    break
            await chosen.click()

        # react-select clears its search input on selection and renders the chosen
        # value in a sibling node, so input_value() is empty even on success.
        # Read the control's rendered text and fall back to the raw input.
        try:
            final = (await field.evaluate("""e => { const c = e.closest('[class*=control]') || e.parentElement; return ((c && c.innerText) || '').trim(); }""")) or ""
        except Exception:
            final = ""
        if not final:
            try:
                final = (await field.input_value()).strip()
            except Exception:
                final = ""
        if final:
            print(f"  [ok] Filled 'location' with {_summarize(final, 40)}")
            return True

    print("  [warn] Location field did not retain a value; left for review")
    return False


# Fields the markup itself declares required. Checked at the review gate so the
# person sees exactly what still needs their input instead of discovering it
# after clicking submit. Only markup-declared requirements are reported - no
# guessing from asterisks in label text, which produces false positives.
PLACEHOLDER_LABEL_RE = re.compile(r"^(select\.*|choose\.*|--+)$", re.IGNORECASE)

REQUIRED_FIELD_SELECTOR = (
    "input[required], input[aria-required='true'], "
    "select[required], select[aria-required='true'], "
    "textarea[required], textarea[aria-required='true']"
)


async def _field_is_empty(field: Locator) -> bool:
    """True if a required field still has no value."""
    tag = (await field.evaluate("e => e.tagName.toLowerCase()")) or ""
    if tag == "select":
        value = await field.evaluate("e => e.value")
        return not value
    field_type = (await field.get_attribute("type") or "").lower()
    if field_type == "file":
        return not await field.evaluate("e => e.files && e.files.length > 0")
    if field_type in ("checkbox", "radio"):
        return False  # handled by check_required_checkboxes / work-auth logic
    try:
        if (await field.input_value()).strip():
            return False
    except Exception:
        return False
    # Only a combobox hides its value outside the input. For a plain text input
    # an empty input_value() means empty - falling back to container text here
    # picks up the field's own LABEL and silently reports it as filled.
    if (await field.get_attribute("role")) != "combobox":
        return True
    try:
        rendered = (await field.evaluate("""e => { const c = e.closest('[class*=control]') || e.parentElement; return ((c && c.innerText) || '').trim(); }""")) or ""
    except Exception:
        rendered = ""
    normalized = " ".join(rendered.split())
    if not normalized:
        return True
    # An unselected combobox control renders only its placeholder.
    return bool(PLACEHOLDER_LABEL_RE.match(normalized))


async def report_unfilled_required_fields(page: Page) -> list[str]:
    """List required fields still empty, so the review gate names them."""
    unfilled: list[str] = []
    fields = page.locator(REQUIRED_FIELD_SELECTOR)
    for index in range(min(await fields.count(), 120)):
        field = fields.nth(index)
        if not await field.is_visible():
            continue
        if not await _field_is_empty(field):
            continue
        label = (await _checkbox_label_text(page, field)) or ""
        label = " ".join(label.split())
        if PLACEHOLDER_LABEL_RE.match(label):
            continue  # the visible half of a combobox already reported by label
        entry = _summarize(label or (await field.get_attribute("name")) or "unnamed field")
        if entry not in unfilled:
            unfilled.append(entry)

    # Checkbox and radio inputs are skipped above because one unchecked box says
    # nothing - the GROUP is the unit of completeness. A required group with
    # nothing selected does block submission, so report it here. Without this the
    # gate printed "No required fields left empty" on forms where it had just
    # deliberately left a required consent box or multi-select for the person.
    groups = page.locator(
        "input[type='checkbox'][required], input[type='checkbox'][aria-required='true'], "
        "input[type='radio'][required], input[type='radio'][aria-required='true']"
    )
    seen: set[str] = set()
    for index in range(min(await groups.count(), 120)):
        box = groups.nth(index)
        if not await box.is_visible():
            continue
        name = (await box.get_attribute("name")) or ""
        key = name or f"__anon{index}"
        if key in seen:
            continue
        seen.add(key)
        try:
            if name:
                if await page.locator(f"input[name='{name}']:checked").count():
                    continue
            elif await box.is_checked():
                continue
        except Exception:
            continue
        label = " ".join(((await _checkbox_label_text(page, box)) or "").split())
        entry = _summarize(label or name or "unnamed group")
        if entry not in unfilled:
            unfilled.append(entry)

    if unfilled:
        print(f"  [review] {len(unfilled)} required field(s) still need you:")
        for item in unfilled:
            print(f"      - {item}")
    else:
        print("  [ok] No required fields left empty")
    return unfilled


# ---------------------------------------------------------------------------
# Work authorization / sponsorship on custom (react-select) comboboxes
# ---------------------------------------------------------------------------
# handle_work_authorization walks fieldsets containing radios/selects, which
# misses ATS forms that render these questions as custom comboboxes. Phrasing
# also varies far more than the original keyword list allowed - Reddit asks
# "require immigration sponsorship" with no mention of a visa.
AUTHORIZATION_QUESTION_RE = re.compile(
    r"(authoriz(?:ed|ation)\s+to\s+work|legally\s+authoriz|eligible\s+to\s+work|"
    r"right\s+to\s+work|work\s+authorization)",
    re.IGNORECASE,
)
SPONSORSHIP_QUESTION_RE = re.compile(
    # "sponsor" also covers "sponsorship"; Stripe phrases it as
    # "require Stripe to sponsor you for a work permit".
    r"((?:require|need|request).{0,40}(?:sponsor|visa|immigration|work\s+permit)|"
    r"(?:immigration|visa)\s+sponsor|sponsorship\s+now\s+or\s+in\s+the\s+future)",
    re.IGNORECASE,
)

# Walk up from the control to the first ancestor carrying real question text.
# Greenhouse wires react-select to a real <label for> / aria-labelledby, the
# only reliable source for a SHORT label: the ancestor walk below needs >12
# chars of text and so steps straight past "Country", returning an unrelated
# ancestor line that no anchored regex can match. Checked alongside the
# ancestor label rather than replacing it, so long-question matching that
# already works stays untouched.
EXPLICIT_LABEL_JS = r"""e => {
  const byId = (id) => {
    const n = id && document.getElementById(id);
    return (n && n.innerText) ? n.innerText.trim() : '';
  };
  const ref = e.getAttribute('aria-labelledby');
  if (ref) {
    const t = ref.split(/\s+/).map(byId).filter(Boolean).join(' ').trim();
    if (t) return t.split('\n')[0];
  }
  const al = e.getAttribute('aria-label');
  if (al && al.trim()) return al.trim();
  if (e.id) {
    const l = document.querySelector('label[for="' + e.id + '"]');
    if (l && l.innerText) return l.innerText.trim().split('\n')[0];
  }
  return '';
}"""


COMBOBOX_LABEL_JS = r"""e => {
  let n = e, label = '';
  for (let k = 0; k < 6 && n; k++) {
    n = n.parentElement;
    if (n && n.innerText && n.innerText.trim().length > 12) { label = n.innerText.trim(); break; }
  }
  return label.split('\n')[0];
}"""


SELECT_LABEL_JS = r"""e => {
  if (e.id) {
    const l = document.querySelector('label[for="' + e.id + '"]');
    if (l && l.innerText) return l.innerText.trim().split('\n')[0];
  }
  const al = e.getAttribute('aria-label');
  if (al) return al.trim();
  let n = e, label = '';
  for (let k = 0; k < 6 && n; k++) {
    n = n.parentElement;
    if (n && n.innerText && n.innerText.trim().length > 2) { label = n.innerText.trim(); break; }
  }
  return label.split('\n')[0];
}"""


async def answer_native_select(page: Page, label_re, option_res: list, exclude_re=None) -> int:
    """
    Choose an option in a native <select> whose label matches label_re.

    Greenhouse renders most dropdowns as react-select comboboxes, so every
    other handler here looks for input[role='combobox'] and cannot see a plain
    <select> at all - which is how Stripe's embedded phone-country field stayed
    empty while the review gate correctly reported it required. option_res is
    tried in order so an exact country name wins over a bare dialling code
    (both "+1" and "United States +1" appear, and Canada is also +1).
    Selects that already hold a value are left alone.
    """
    answered = 0
    selects = page.locator("select")
    for index in range(min(await selects.count(), 40)):
        el = selects.nth(index)
        if not await el.is_visible():
            continue
        try:
            if await el.evaluate("e => !!e.value"):
                continue
            label = await el.evaluate(SELECT_LABEL_JS)
            texts = await el.evaluate(
                "e => Array.from(e.options).map(o => (o.textContent || '').trim())"
            )
        except Exception:
            continue
        if not label or not label_re.search(label):
            continue
        if exclude_re is not None and exclude_re.search(label):
            continue
        chosen = None
        for option_re in option_res:
            for position, text in enumerate(texts):
                if text and option_re.search(text):
                    chosen = (position, text)
                    break
            if chosen:
                break
        if chosen is None:
            continue
        try:
            await el.select_option(index=chosen[0])
        except Exception:
            continue
        print(f"  [ok] Selected '{_summarize(label, 40)}' -> {_summarize(chosen[1], 30)}")
        answered += 1
    return answered


async def answer_labeled_combobox(
    page: Page, label_re, answers: list[str] | None = None, option_re=None,
    search_text: str = "", exclude_re=None, all_matches: bool = False,
) -> bool:
    """
    Answer a custom combobox whose nearby label matches label_re.

    Options are scoped to the control's own aria-controls listbox so a match
    cannot land in an unrelated dropdown, and the menu is waited for rather
    than assumed - react-select needs ~1.2s to mount it.
    """
    answered_any = False
    boxes = page.locator("input[role='combobox']")
    for index in range(min(await boxes.count(), 40)):
        box = boxes.nth(index)
        if not await box.is_visible():
            continue
        try:
            label = await box.evaluate(COMBOBOX_LABEL_JS)
            explicit = await box.evaluate(EXPLICIT_LABEL_JS)
        except Exception:
            continue
        # Either source may carry the question: a short label appears only in
        # the explicit one, a long question only in the ancestor block.
        if explicit and label_re.search(explicit):
            label = explicit
        elif not label or not label_re.search(label):
            continue
        # A question can match more than one topic - Elastic asks "Will you
        # require Elastic's sponsorship to continue or extend your work
        # authorization status?", which reads as both. exclude_re lets the
        # more specific handler claim it so the wrong answer is never given.
        if exclude_re is not None and exclude_re.search(label):
            continue

        await box.click()
        if search_text:
            # Typeahead lists (School) only render options once filtered.
            await box.fill(search_text)
            await page.wait_for_timeout(1200)
        listbox_id = await box.get_attribute("aria-controls")
        options = (
            page.locator(f"[id='{listbox_id}'] [role='option']")
            if listbox_id
            else page.get_by_role("option")
        )
        try:
            await options.first.wait_for(state="visible", timeout=5000)
        except Exception:
            await page.keyboard.press("Escape")
            continue

        # Snapshot option text once: clicking closes the menu, so any further
        # click attempt against this list would hang until timeout.
        texts: list[str] = []
        for opt_index in range(min(await options.count(), 40)):
            try:
                texts.append(((await options.nth(opt_index).inner_text()) or "").strip())
            except Exception:
                texts.append("")

        chosen_index = -1
        if option_re is not None:
            for opt_index, text in enumerate(texts):
                if text and option_re.search(text):
                    chosen_index = opt_index
                    break
        else:
            # Exact matches win over prefix matches: a short answer like "US"
            # must not select "US Citizen" when a literal "US" option exists.
            for match_exact in (True, False):
                for want in (answers or []):
                    for opt_index, text in enumerate(texts):
                        low, wl = text.lower(), want.lower()
                        if (low == wl) if match_exact else low.startswith(wl):
                            chosen_index = opt_index
                            break
                    if chosen_index >= 0:
                        break
                if chosen_index >= 0:
                    break

        if chosen_index >= 0:
            await options.nth(chosen_index).click()
            print(f"  [ok] Answered '{_summarize(label, 46)}' -> {_summarize(texts[chosen_index], 30)}")
            if not all_matches:
                return True
            answered_any = True
            continue  # menu is closed; move to the next control

        await page.keyboard.press("Escape")
    return answered_any


# Voluntary self-identification. The agent never infers these: it selects the
# form's own decline option only when profile.json says eeo_response ==
# "decline", and otherwise leaves them entirely for the applicant.
EEO_QUESTION_RES = [
    re.compile(r"\bgender\b", re.IGNORECASE),
    re.compile(r"transgender", re.IGNORECASE),
    re.compile(r"sexual\s+orientation", re.IGNORECASE),
    re.compile(r"disabilit", re.IGNORECASE),
    re.compile(r"veteran|military\s+service", re.IGNORECASE),
    re.compile(r"ethnicit|race|hispanic|latin", re.IGNORECASE),
    # Pronouns are self-identification too; declining is an offered choice.
    re.compile(r"\bpronouns?\b", re.IGNORECASE),
]
DECLINE_OPTION_RE = re.compile(
    r"(don'?t\s+(wish|want)\s+to\s+(answer|disclose|say)|"
    r"do\s+not\s+(wish|want)\s+to\s+(answer|disclose|say)|"
    r"decline\s+to\s+(self[-\s]?identify|answer|disclose)|prefer\s+not\s+to\s+(say|answer|disclose)|"
    # Federal form CC-305 words its decline as "I do not want to answer".
    r"choose\s+not\s+to\s+(disclose|answer)|i\s+do\s+not\s+wish)",
    re.IGNORECASE,
)
SCHOOL_QUESTION_RE = re.compile(r"\bschool\b|\buniversity\b|\bcollege\b", re.IGNORECASE)
DEGREE_QUESTION_RE = re.compile(r"\bdegree\b", re.IGNORECASE)


RADIO_GROUPS_JS = r"""() => {
  const radios = Array.from(document.querySelectorAll('input[type=radio]'));
  const text = e => (e?.innerText || '').trim();
  const groups = {};
  radios.forEach((r, i) => {
    if (!r.offsetParent) return;
    const key = r.name || (r.closest('fieldset') && ('fs' + radios.indexOf(r.closest('fieldset').querySelector('input[type=radio]'))));
    if (!key) return;
    let label = (r.id && text(document.querySelector('label[for="' + CSS.escape(r.id) + '"]'))) ||
                r.getAttribute('aria-label') || text(r.closest('label')) || text(r.parentElement);
    if (!groups[key]) {
      const fs = r.closest('fieldset');
      const q = text(fs?.querySelector('legend')) || text(fs).split('\n')[0] || '';
      groups[key] = {question: q, options: []};
    }
    groups[key].options.push({index: i, label: label.split('\n')[0], checked: r.checked});
  });
  return Object.values(groups);
}"""


async def answer_eeo_radios(page: Page) -> int:
    """
    Pick the decline option in EEO radio groups (OpenAI's Ashby form asks
    Gender/Race/Veteran as radios, which the combobox pass cannot see).
    A disclosure already selected is overwritten, same as the comboboxes.
    """
    answered = 0
    radios = page.locator("input[type='radio']")
    for group in await page.evaluate(RADIO_GROUPS_JS):
        question = group["question"]
        if not any(question_re.search(question) for question_re in EEO_QUESTION_RES):
            continue
        decline = next(
            (o for o in group["options"] if DECLINE_OPTION_RE.search(o["label"])), None
        )
        if decline is None:
            print(f"  [review] No decline option for EEO question: {_summarize(question, 50)}")
            continue
        if not decline["checked"]:
            await radios.nth(decline["index"]).check()
        answered += 1
        print(f"  [ok] Answered '{_summarize(question, 40)}' -> {_summarize(decline['label'], 40)}")
    return answered


async def answer_eeo_comboboxes(page: Page, profile: dict) -> int:
    """Select the form's own decline option on EEO questions, if opted in."""
    if str(profile.get("eeo_response", "")).lower() != "decline":
        return 0
    answered = 0
    for question_re in EEO_QUESTION_RES:
        # all_matches is essential here, not an optimisation: a form can ask the
        # same topic twice - Affirm carries an optional demographic survey ("How
        # do you identify? (gender identity)") AND the federal self-ID block
        # ("Gender"). Stopping at the first match declined the survey and left
        # the government field untouched, so whatever sat in it stood. These
        # comboboxes are also overwritten, not just filled when empty, so a
        # stale disclosure is cleared rather than inherited.
        if await answer_labeled_combobox(
            page, question_re, option_re=DECLINE_OPTION_RE, all_matches=True
        ):
            answered += 1
    answered += await answer_eeo_radios(page)
    if answered:
        # Topics, not fields: one topic can cover two questions on a form that
        # asks it twice (Affirm's survey plus the federal block). Counting
        # topics here understates the work rather than overstating it.
        print(f"  [ok] Declined to self-identify on {answered} EEO topic(s)")
    return answered


async def fill_education_fields(page: Page, profile: dict) -> int:
    """
    Fill School/Degree from profile.json without inventing credentials.

    School tries the real school name first, then the profile's fallbacks
    (e.g. a generic "Coding Bootcamp Graduate" entry) - never a university
    that was not attended. Degree only ever picks from degree_preferences.
    """
    filled = 0
    school_candidates = [profile.get("school", "")] + list(profile.get("school_fallbacks", []))
    for candidate in [c for c in school_candidates if c]:
        if await answer_labeled_combobox(
            page, SCHOOL_QUESTION_RE, answers=[candidate], search_text=candidate
        ):
            filled += 1
            break

    degrees = list(profile.get("degree_preferences", []))
    if degrees and await answer_labeled_combobox(page, DEGREE_QUESTION_RE, answers=degrees):
        filled += 1
    return filled

# Logistics questions whose answers come straight from profile.json. Marketing
# opt-ins (WhatsApp/SMS recruiting) are deliberately excluded - those are
# consent, not facts, and stay with the applicant.
# Recruiter messaging consent, answered only from an explicit profile setting.
RECRUITING_OPTIN_RE = re.compile(
    r"opt[- ]?in.{0,40}(message|text|sms|whatsapp)|"
    r"(whatsapp|sms|text)\s+messages?\s+from|"
    r"receive\s+(text|sms|whatsapp)\s+message",
    re.IGNORECASE,
)

COUNTRY_QUESTION_RE = re.compile(
    r"country\s+(where\s+you\s+currently\s+reside|of\s+residence)|current\s+country|"
    # Brex: "What country are you based in?"
    r"country\s+(are|do)\s+you\s+(currently\s+)?(based|live|reside)", re.IGNORECASE
)
PRIOR_EMPLOYMENT_RE = re.compile(
    # ",?" because Brex writes "have you previously, worked at Capital One".
    r"(ever|previously)\s+(been\s+)?employed\s+(by|at|with)|previously,?\s+worked\s+(at|for)|former\s+employee",
    re.IGNORECASE,
)
REMOTE_PLAN_RE = re.compile(
    r"(plan|intend)\s+to\s+work\s+remotely|work\s+from\s+a\s+remote\s+location", re.IGNORECASE
)
# Relocation is checked before in-office: Brex's relocation question also says
# "to meet this in-office requirement", so in-office excludes it.
RELOCATION_QUESTION_RE = re.compile(r"\brelocat", re.IGNORECASE)
IN_OFFICE_QUESTION_RE = re.compile(
    r"in[- ]office|on[- ]?site\s+(work|requirement|role)|hybrid.{0,40}(days?|week)|"
    r"days\s+(per|a)\s+week\s+in\s+(the\s+)?office",
    re.IGNORECASE,
)
# Choices like "Yes, I'd relocate prior to the start of the role".
RELOCATE_OPTION_RE = re.compile(r"\brelocat", re.IGNORECASE)
NO_OPTION_RE = re.compile(r"^\s*no\b", re.IGNORECASE)


async def answer_logistics_questions(page: Page, profile: dict) -> int:
    """Answer country / prior-employment / remote-intent questions from profile.json."""
    answered = 0

    country = profile.get("country", "")
    if country:
        # Forms spell the US several ways; try the profile value first.
        variants = [country, "United States of America", "United States", "USA", "US"]
        seen, ordered = set(), []
        for v in variants:
            if v and v not in seen:
                seen.add(v); ordered.append(v)
        if await answer_labeled_combobox(page, COUNTRY_QUESTION_RE, answers=ordered):
            answered += 1

    if "previously_employed_at_target" in profile:
        want = "Yes" if profile["previously_employed_at_target"] else "No"
        if await answer_labeled_combobox(page, PRIOR_EMPLOYMENT_RE, answers=[want]):
            answered += 1


    if "recruiting_messages_opt_in" in profile:
        want = "Yes" if profile["recruiting_messages_opt_in"] else "No"
        if await answer_labeled_combobox(page, RECRUITING_OPTIN_RE, answers=[want]):
            answered += 1
    state = profile.get("state", "")
    if state and await answer_labeled_combobox(page, STATE_QUESTION_RE, answers=[state]):
        answered += 1

    if profile.get("job_source"):
        if await answer_labeled_combobox(
            page, JOB_SOURCE_QUESTION_RE, option_re=JOB_SOURCE_OPTION_RE
        ):
            answered += 1

    if profile.get("previously_employed_at_target") is False:
        if await answer_labeled_combobox(
            page, PRIOR_EMPLOYMENT_RE, option_re=NO_PRIOR_EMPLOYMENT_OPTION_RE, all_matches=True
        ):
            answered += 1

    if "sanctioned_country_ties" in profile:
        want = "Yes" if profile["sanctioned_country_ties"] else "No"
        if await answer_labeled_combobox(page, SANCTIONED_COUNTRY_RE, answers=[want]):
            answered += 1

    if "plans_to_work_remotely" in profile:
        want = "Yes" if profile["plans_to_work_remotely"] else "No"
        if await answer_labeled_combobox(page, REMOTE_PLAN_RE, answers=[want]):
            answered += 1

    # None/absent means "ask me each time": the question is left for review.
    relocate = profile.get("willing_to_relocate")
    in_office = profile.get("open_to_in_office")
    if relocate is not None:
        if relocate:
            # Prefer the explicit relocation choice over a bare "Yes, I live here".
            done = await answer_labeled_combobox(
                page, RELOCATION_QUESTION_RE, option_re=RELOCATE_OPTION_RE
            ) or await answer_labeled_combobox(page, RELOCATION_QUESTION_RE, answers=["Yes"])
        else:
            done = await answer_labeled_combobox(
                page, RELOCATION_QUESTION_RE, option_re=NO_OPTION_RE
            )
        answered += int(bool(done))
    if in_office is not None:
        if in_office:
            done = False
            if relocate:
                done = await answer_labeled_combobox(
                    page, IN_OFFICE_QUESTION_RE, option_re=RELOCATE_OPTION_RE,
                    exclude_re=RELOCATION_QUESTION_RE,
                )
            done = done or await answer_labeled_combobox(
                page, IN_OFFICE_QUESTION_RE, answers=["Yes"], exclude_re=RELOCATION_QUESTION_RE
            )
        else:
            done = await answer_labeled_combobox(
                page, IN_OFFICE_QUESTION_RE, option_re=NO_OPTION_RE,
                exclude_re=RELOCATION_QUESTION_RE,
            )
        answered += int(bool(done))

    return answered

# "Are you located in the US or Canada?" is Yes only for residents of either.
NORTH_AMERICA_COUNTRY_RE = re.compile(r"^(united\s+states|usa?\b|canada)", re.IGNORECASE)
# Short free-text questions answerable straight from profile.json.
TEXT_QUESTION_RULES = [
    (re.compile(r"current\s+or\s+previous\s+job\s+title|current\s+job\s+title|"
                r"most\s+recent\s+job\s+title", re.IGNORECASE), "current_job_title"),
    (re.compile(r"current\s+or\s+previous\s+employer|current\s+employer|"
                r"most\s+recent\s+(company|employer)|current\s+company|"
                r"name\s+of\s+your\s+current", re.IGNORECASE),
     "current_employer"),
    (re.compile(r"city\s+and\s+state|what\s+city.*reside|city/state", re.IGNORECASE), "city_state"),
    (re.compile(r"primary\s+(programming\s+)?language|main\s+(programming\s+)?language",
                re.IGNORECASE), "primary_language"),
    # Some ATS name these question_<id> with the text only in the label, so
    # selector matching on id/name/aria-label finds nothing.
    (re.compile(r"preferred\s+(first\s+)?name", re.IGNORECASE), "preferred_name"),
    (re.compile(r"linkedin", re.IGNORECASE), "linkedin_url"),
    # GitHub must be matched before the portfolio rule and kept out of it:
    # a recruiter clicking "GitHub" expects the code profile, not the site.
    (re.compile(r"\bgit\s?hub\b", re.IGNORECASE), "github_url"),
    (re.compile(r"\bportfolio\b|personal\s+website", re.IGNORECASE), "portfolio_url"),
    (re.compile(r"countr(y|ies).{0,40}(right\s+to\s+work|authorized\s+to\s+work)|"
                r"unrestricted\s+right\s+to\s+work", re.IGNORECASE), "work_rights_text"),
    (re.compile(r"how\s+did\s+you\s+(first\s+)?(hear|learn)\s+about|"
                r"where\s+did\s+you\s+(hear|find)", re.IGNORECASE), "job_source"),
    (re.compile(r"when\s+(can|could|would)\s+you\s+start|earliest\s+(possible\s+)?start|"
                r"\bstart\s+date\b|availab(le|ility)\s+to\s+start|notice\s+period",
                re.IGNORECASE), "start_availability"),
]

# A bare "Country" combobox next to the phone input is the dialling-code
# selector (options read "United States +1"), NOT country of residence - the
# residence question is handled by COUNTRY_QUESTION_RE.
PHONE_COUNTRY_RE = re.compile(r"^\s*country\s*\*?\s*$", re.IGNORECASE)
# The native-select variant may carry the word "Phone" into its resolved label.
PHONE_COUNTRY_SELECT_RE = re.compile(
    r"^\s*(phone\s*)?country\s*(code)?\s*\*?\s*$", re.IGNORECASE
)
PHONE_US_EXACT_RE = re.compile(r"^\s*united\s+states(\s+of\s+america)?\b", re.IGNORECASE)
PHONE_US_DIAL_RE = re.compile(r"^\s*\+?1\s*$|\(\+1\)", re.IGNORECASE)

# Some questions offer full-sentence choices rather than Yes/No, so the answer
# is matched by regex against the option text instead of by value.
STATE_QUESTION_RE = re.compile(
    r"(which\s+)?(u\.?s\.?\s+)?state(\s+or\s+canadian\s+province)?\s+(do\s+you\s+)?reside|"
    r"state\s+of\s+residence", re.IGNORECASE
)
JOB_SOURCE_QUESTION_RE = re.compile(
    r"how\s+did\s+you\s+(first\s+)?(hear|learn)\s+about|where\s+did\s+you\s+(hear|find)",
    re.IGNORECASE,
)
# "Company Website" shows up as "<Company>'s Career Site", "Company website", etc.
JOB_SOURCE_OPTION_RE = re.compile(
    r"career\s+(site|page)|company\s+(web)?site|company\s+career|job\s+board", re.IGNORECASE
)
# "No" is often phrased as a sentence: "I have not previously been employed at X".
NO_PRIOR_EMPLOYMENT_OPTION_RE = re.compile(
    r"^\s*no\b|have\s+not\s+(previously\s+)?been\s+employed|never\s+(been\s+)?employed",
    re.IGNORECASE,
)
PRONOUN_QUESTION_RE = re.compile(r"\bpronouns?\b", re.IGNORECASE)

# Export-control screening: "are you a citizen or resident of Cuba, Iran, ...".
SANCTIONED_COUNTRY_RE = re.compile(
    r"citizen\s+or\s+resident\s+of\s+any\s+of\s+the\s+following|"
    r"cuba,?\s*iran|north\s+korea.{0,40}(syria|crimea|russia)",
    re.IGNORECASE,
)

# Timezone questions ("Do you reside in EST?") answered from profile["timezone"].
TIMEZONE_QUESTION_RE = re.compile(r"(reside|located|based).{0,30}(time\s?zone|est|pst|cst|mst)|"
                                  r"time\s?zone", re.IGNORECASE)
# Pre-written answers to recurring essay prompts, drawn from the applicant's
# own resume and candidate_context.md. These fill without an LLM call, so the
# review gate stays complete even when the Gemini quota is exhausted; the
# Gemini drafting pass still handles anything not matched here.
PREPARED_ANSWERS_PATH = BASE_DIR / "prepared_answers.json"
ACKNOWLEDGE_QUESTION_RE = re.compile(
    r"i\s+acknowledge|acknowledge\s+and\s+submit|privacy\s+(statement|notice|policy)|"
    # Reddit words the same attestation as "By selecting \"I agree\", I understand..."
    r"by\s+selecting\s+['\"]?i\s+agree|i\s+agree[,\"']?\s+i\s+understand|"
    # Brex: "Do you consent to Brex processing your personal information..."
    r"consent\s+to\s+.{0,60}processing\s+(of\s+)?your\s+personal",
    re.IGNORECASE,
)


def load_prepared_answers() -> list[dict]:
    data = load_json(PREPARED_ANSWERS_PATH, [])
    return data if isinstance(data, list) else []


# Skill screeners -> profile["skill_answers"] keys. A question with no mapped
# key, or a key absent from the profile, is left for the applicant.
SKILL_QUESTION_RULES = [
    (re.compile(r"agentic\s+workflow|multi-?agent|langgraph|langchain|autogen", re.IGNORECASE),
     "agentic_workflows"),
    (re.compile(r"terraform|infrastructure[- ]as[- ]code|\bIaC\b", re.IGNORECASE),
     "infrastructure_as_code"),
    (re.compile(r"proficient\s+in\s+python|python\s+or\s+typescript|"
                r"experience\s+with\s+python", re.IGNORECASE), "python_or_typescript"),
    (re.compile(r"\bRAG\b|retrieval[- ]augmented|vector\s+search|pinecone|weaviate|"
                r"relevance\s+engine", re.IGNORECASE), "rag_vector_search"),
]

# Contact-preference checkbox groups.
CONTACT_METHOD_RE = re.compile(r"how\s+should\s+we\s+communicate|preferred\s+(method|means)\s+of\s+contact|"
                               r"how\s+would\s+you\s+like\s+to\s+be\s+contacted", re.IGNORECASE)
# "Which countries will you work in?" is a required multi-select; the answer is
# a fact from profile.json rather than a consent tick, so it can be answered.
WORK_COUNTRIES_RE = re.compile(
    r"countr(y|ies)\s+you\s+anticipate\s+working|countries?\s+you\s+(will|plan\s+to)\s+work",
    re.IGNORECASE,
)


async def fill_profile_text_questions(page: Page, profile: dict) -> int:
    """Fill short free-text questions whose answer lives in profile.json."""
    filled = 0
    inputs = page.locator("input[type='text'], input:not([type])")
    for index in range(min(await inputs.count(), 120)):
        field = inputs.nth(index)
        if not await field.is_visible():
            continue
        if await field.get_attribute("role") == "combobox":
            continue
        try:
            if (await field.input_value()).strip():
                continue
        except Exception:
            continue
        label = " ".join(((await _checkbox_label_text(page, field)) or "").split())
        if not label:
            # Ashby custom questions ("When can you start a new role?") are
            # invisible to the checkbox-style lookup; use the label[for] /
            # aria-labelledby and ancestor-text lookups the comboboxes use.
            try:
                label = (await field.evaluate(EXPLICIT_LABEL_JS)) or (
                    await field.evaluate(COMBOBOX_LABEL_JS)
                ) or ""
            except Exception:
                label = ""
            label = " ".join(label.split())
        if not label:
            continue
        for pattern, key in TEXT_QUESTION_RULES:
            value = profile.get(key, "")
            if value and pattern.search(label):
                await field.fill(str(value))
                print(f"  [ok] Answered '{_summarize(label, 44)}' -> {_summarize(str(value), 30)}")
                filled += 1
                break
    return filled


async def select_checkbox_group(page: Page, group_re, wanted_values: list[str]) -> int:
    """Tick named options inside a checkbox group whose legend matches group_re."""
    wanted = [str(c).strip().lower() for c in wanted_values if str(c).strip()]
    if not wanted:
        return 0
    checked = 0
    boxes = page.locator("input[type='checkbox']")
    for index in range(min(await boxes.count(), 120)):
        box = boxes.nth(index)
        if not await box.is_visible() or await box.is_checked():
            continue
        group_label = ""
        try:
            group_label = await box.evaluate(
                "e => { const f = e.closest('fieldset,[role=group]');"
                " return f ? (f.querySelector('legend')?.textContent || '') : ''; }"
            )
        except Exception:
            pass
        if not group_label or not group_re.search(group_label):
            continue
        label = " ".join(((await _checkbox_label_text(page, box)) or "").split()).lower()
        if label in wanted:
            await box.check()
            checked += 1
            print(f"  [ok] Checkbox selected: {label}")
    return checked


async def select_profile_checkbox_groups(page: Page, profile: dict) -> int:
    """Answer checkbox-group questions whose values come from profile.json."""
    total = 0
    total += await select_checkbox_group(page, WORK_COUNTRIES_RE, profile.get("work_countries", []))
    total += await select_checkbox_group(
        page, CONTACT_METHOD_RE, profile.get("preferred_contact_methods", [])
    )
    return total


async def fill_prepared_answers(page: Page) -> int:
    """Fill long-form prompts from prepared_answers.json (no LLM required)."""
    rules = load_prepared_answers()
    if not rules:
        return 0
    compiled = []
    for rule in rules:
        pattern, answer = rule.get("pattern", ""), rule.get("answer", "")
        if pattern and answer:
            compiled.append((re.compile(pattern, re.IGNORECASE), answer))

    filled = 0
    areas = page.locator("textarea")
    for index in range(min(await areas.count(), 60)):
        area = areas.nth(index)
        if not await area.is_visible():
            continue
        try:
            if (await area.input_value()).strip():
                continue
        except Exception:
            continue
        label = " ".join(((await _checkbox_label_text(page, area)) or "").split())
        for pattern, answer in compiled:
            if pattern.search(label):
                await area.fill(answer)
                print(f"  [ok] Drafted answer for '{_summarize(label, 46)}' ({len(answer)} chars)")
                filled += 1
                break
    return filled


async def answer_acknowledgements(page: Page, profile: dict) -> int:
    """Select 'I acknowledge' on privacy-statement controls, if opted in."""
    if not profile.get("acknowledge_privacy_statements"):
        return 0
    if await answer_labeled_combobox(
        page, ACKNOWLEDGE_QUESTION_RE,
        answers=["I acknowledge", "Acknowledge", "Yes", "I agree", "Consent", "I consent"],
        all_matches=True,
    ):
        return 1
    return 0


async def answer_skill_questions(page: Page, profile: dict) -> int:
    """Answer skill screeners from profile['skill_answers']; skip unmapped ones."""
    answers = profile.get("skill_answers", {}) or {}
    if not answers:
        return 0
    answered = 0
    for pattern, key in SKILL_QUESTION_RULES:
        value = answers.get(key)
        if not value:
            continue
        if await answer_labeled_combobox(page, pattern, answers=[str(value)], all_matches=True):
            answered += 1
    return answered


async def answer_phone_country(page: Page, profile: dict) -> int:
    """Select the phone dialling-code country from profile['phone_country']."""
    country = str(profile.get("phone_country", "")).strip()
    if not country:
        return 0
    if await answer_labeled_combobox(page, PHONE_COUNTRY_RE, answers=[country]):
        return 1
    # Embedded forms render this as a plain <select> of dialling codes.
    if await answer_native_select(
        page, PHONE_COUNTRY_SELECT_RE, [PHONE_US_EXACT_RE, PHONE_US_DIAL_RE]
    ):
        return 1
    return 0


async def answer_timezone_question(page: Page, profile: dict) -> int:
    """Answer "do you reside in <TZ>?" using profile['timezone']."""
    tz = str(profile.get("timezone", "")).strip()
    if not tz:
        return 0
    boxes = page.locator("input[role='combobox']")
    for index in range(min(await boxes.count(), 40)):
        box = boxes.nth(index)
        if not await box.is_visible():
            continue
        try:
            label = await box.evaluate(COMBOBOX_LABEL_JS)
        except Exception:
            continue
        if not label or not TIMEZONE_QUESTION_RE.search(label):
            continue
        # Only answer when the question names the timezone the profile declares.
        want = "Yes" if re.search(rf"\b{re.escape(tz)}\b", label, re.IGNORECASE) else None
        if want is None:
            print(f"  [review] Timezone question names a different zone: {_summarize(label, 44)}")
            return 0
        if await answer_labeled_combobox(page, TIMEZONE_QUESTION_RE, answers=[want]):
            return 1
    return 0

async def handle_authorization_comboboxes(page: Page, profile: dict) -> int:
    """Answer work-authorization and sponsorship comboboxes from profile.json."""
    answered = 0
    authorized = bool(profile.get("authorized_to_work_us", False))
    sponsorship = bool(profile.get("requires_sponsorship", False))

    # Sponsorship is checked first and authorization explicitly skips any
    # sponsorship-worded question: answering "Yes, I am authorized" into a
    # "do you require sponsorship?" control is a materially wrong answer.
    if await answer_labeled_combobox(
        page, SPONSORSHIP_QUESTION_RE, ["Yes"] if sponsorship else ["No"],
        all_matches=True,
    ):
        answered += 1
    if await answer_labeled_combobox(
        page, AUTHORIZATION_QUESTION_RE, ["Yes"] if authorized else ["No"],
        exclude_re=SPONSORSHIP_QUESTION_RE, all_matches=True,
    ):
        answered += 1

    # The same questions rendered as plain <select> elements, which the
    # combobox pass above cannot see.
    def yes_no(value: bool) -> list:
        return [re.compile(r"^\s*yes\b" if value else r"^\s*no\b", re.IGNORECASE)]

    answered += await answer_native_select(page, SPONSORSHIP_QUESTION_RE, yes_no(sponsorship))
    answered += await answer_native_select(
        page, AUTHORIZATION_QUESTION_RE, yes_no(authorized), exclude_re=SPONSORSHIP_QUESTION_RE
    )
    return answered



async def repair_cleared_fields(page: Page, profile: dict) -> int:
    """
    Re-fill contact/text fields that a later step wiped.

    Selecting a location on some Greenhouse forms (Elastic) re-mounts the form
    and clears first/last/email that were filled earlier, so the run logs a
    successful fill while the field ends up blank. Re-filling at the end is
    cheap and idempotent - fill_contact_fields overwrites, and the text-question
    pass skips anything already populated.
    """
    email = await first_visible(page, FIELD_SELECTORS["email"])
    needs_repair = False
    if email is not None:
        try:
            needs_repair = not (await email.input_value()).strip()
        except Exception:
            needs_repair = False
    if not needs_repair:
        for key in ("full_name", "first_name"):
            field = await first_visible(page, FIELD_SELECTORS[key])
            if field is None:
                continue
            try:
                if not (await field.input_value()).strip():
                    needs_repair = True
            except Exception:
                pass
            break

    if not needs_repair:
        # Custom text questions can be wiped on their own (OpenAI's start-date
        # field emptied after a later step while contact fields survived). The
        # text pass only fills blanks, so re-running it is always safe.
        return await fill_profile_text_questions(page, profile)
    print("  [repair] Contact fields were cleared by a later step; re-filling.")
    await fill_contact_fields(page, profile)
    await fill_profile_text_questions(page, profile)
    return 1


async def prepare_job(
    context: BrowserContext, job: dict, profile: dict, semaphore: asyncio.Semaphore
) -> tuple[Page | None, str | None]:
    """
    Prepare one application. Returns (page, unfinished_title).

    The second element names the job when it did NOT reach the review gate, so
    the end-of-run summary can say which tabs are only partially filled instead
    of counting every open tab as prepared.
    """
    url = job["url"]
    title = job.get("title", url)
    finished = False
    async with semaphore:
        page = await context.new_page()
        print(f"\n=== Preparing: {title} ===")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if not await wait_for_challenge_clear(page, title):
                raise RuntimeError("Bot challenge was not cleared in time; tab left open for you")
            await self_heal_action(
                page, "Open application form", lambda: open_application_form(page), url
            )
            # Opening the form can navigate to another host (an apply page or
            # an embedded ATS), which may put up its own challenge.
            if not await wait_for_challenge_clear(page, title):
                raise RuntimeError("Bot challenge was not cleared in time; tab left open for you")
            await self_heal_action(
                page, "Fill contact fields", lambda: fill_contact_fields(page, profile), url
            )
            await self_heal_action(
                page, "Upload resume", lambda: upload_resume(page, profile), url
            )
            await self_heal_action(
                page,
                "Fill profile questions",
                lambda: fill_known_profile_questions(page, profile),
                url,
            )
            await self_heal_action(
                page, "Fill location", lambda: fill_location_field(page, profile), url
            )
            await self_heal_action(
                page,
                "Answer authorization questions",
                lambda: handle_authorization_comboboxes(page, profile),
                url,
            )
            await self_heal_action(
                page, "Fill education", lambda: fill_education_fields(page, profile), url
            )
            await self_heal_action(
                page, "Answer logistics questions",
                lambda: answer_logistics_questions(page, profile), url,
            )
            await self_heal_action(
                page, "Fill profile text questions",
                lambda: fill_profile_text_questions(page, profile), url,
            )
            await self_heal_action(
                page, "Select checkbox groups",
                lambda: select_profile_checkbox_groups(page, profile), url,
            )
            await self_heal_action(
                page, "Fill prepared answers", lambda: fill_prepared_answers(page), url
            )
            await self_heal_action(
                page, "Answer acknowledgements",
                lambda: answer_acknowledgements(page, profile), url,
            )
            await self_heal_action(
                page, "Accept legal acknowledgements",
                lambda: accept_legal_acknowledgements(page, profile), url,
            )
            await self_heal_action(
                page, "Answer skill screeners",
                lambda: answer_skill_questions(page, profile), url,
            )
            await self_heal_action(
                page, "Select phone country", lambda: answer_phone_country(page, profile), url
            )
            await self_heal_action(
                page, "Answer timezone question",
                lambda: answer_timezone_question(page, profile), url,
            )
            await self_heal_action(
                page, "EEO self-identification", lambda: answer_eeo_comboboxes(page, profile), url
            )
            await self_heal_action(
                page,
                "Draft open-ended responses",
                lambda: fill_open_ended_fields(page),
                url,
            )
            await self_heal_action(
                page, "Check required checkboxes", lambda: check_required_checkboxes(page), url
            )
            await self_heal_action(
                page, "Repair cleared fields", lambda: repair_cleared_fields(page, profile), url
            )
            await report_multi_step_form(page)
            await report_unfilled_required_fields(page)
            await report_form_captcha(page)
            await log_status(job, "success")
            await log_status(job, "review_gate_reached")
            finished = True
            print("  [review] Ready for manual completion and submission")
            return page, (None if finished else title)
        except Exception as error:
            await log_status(job, "error", str(error))
            print(f"  [error] {title}: {error}")
            return page, title


async def keep_browser_open(
    browser: Browser, context: BrowserContext, pages: list[Page],
    failed: list[str] | None = None,
) -> None:
    print(f"\nPrepared {len(pages)} tab(s). No submit buttons were clicked.")
    if failed:
        # A job that raised still leaves a tab open, so counting tabs overstated
        # what was done: a half-filled form that never reached the review gate
        # was reported exactly like a finished one.
        print(f"[warn] {len(failed)} of those did NOT finish and are partially "
              f"filled - do not submit without checking:")
        for title in failed:
            print(f"    - {title}")
    print("Browser retention is active. Review and close the browser manually when finished.")
    disconnected = asyncio.Event()
    browser.on("disconnected", lambda _browser: disconnected.set())
    context.on("close", lambda _context: disconnected.set())
    await disconnected.wait()


async def run_batch(queue_path: Path, concurrency: int, ignore_history: bool = False) -> None:
    jobs = load_queue(queue_path, ignore_history=ignore_history)
    if not jobs:
        raise ValueError(f"No valid jobs found in {queue_path}")
    profile = load_json(PROFILE_PATH, None)
    if not isinstance(profile, dict):
        raise FileNotFoundError(
            f"Missing or invalid {PROFILE_PATH.name}. Copy profile.example.json to "
            "profile.json and fill in your details."
        )
    semaphore = asyncio.Semaphore(concurrency)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()

    results = await asyncio.gather(
        *(prepare_job(context, job, profile, semaphore) for job in jobs)
    )
    pages = [r[0] if isinstance(r, tuple) else r for r in results]
    failed = [r[1] for r in results if isinstance(r, tuple) and r[1]]
    open_pages = [page for page in pages if page is not None and not page.is_closed()]
    if open_pages:
        await open_pages[-1].bring_to_front()
    await keep_browser_open(browser, context, open_pages, failed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare queued job applications concurrently for manual review."
    )
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_QUEUE_PATH, help="Queue JSON file"
    )
    parser.add_argument(
        "--concurrency", type=int, default=6, help="Maximum simultaneous tabs"
    )
    parser.add_argument(
        "--challenge-timeout", type=float, default=CHALLENGE_TIMEOUT_SECONDS,
        help="Seconds to wait for you to solve a CAPTCHA/Cloudflare check before "
             "giving up on that job (default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-history",
        action="store_true",
        help="Re-prepare jobs already handled in a previous run (e.g. after form-filling improvements)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.concurrency <= 20:
        raise SystemExit("--concurrency must be between 1 and 20")
    global CHALLENGE_TIMEOUT_SECONDS
    CHALLENGE_TIMEOUT_SECONDS = max(0.0, args.challenge_timeout)
    try:
        asyncio.run(run_batch(args.file, args.concurrency, args.ignore_history))
    except KeyboardInterrupt:
        print("\nRunner stopped. Close any remaining browser windows manually.")
    except (FileNotFoundError, ValueError) as error:
        print(f"[error] {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
