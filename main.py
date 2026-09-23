"""
Job application autofill agent.

Uses Playwright to open a job application page, fill in common fields
(first/last name, email, phone), upload a resume, answer work-authorization
questions, then pause for manual review before submission. Every processed
URL is logged to applications_log.json and applications_log.csv.

Supports ATS platforms with differing DOM structures (Ashby, Greenhouse,
Lever, Workday, and generic fallbacks) via a selector-fallback config.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

from candidate_answers import draft_answers, is_eligible_open_question
from application_history import load_handled_urls
from bot_challenge import BLOCKING_CHALLENGE_JS

try:
    from playwright_stealth import Stealth
except ImportError:
    Stealth = None

from sheets_sync import sync_logs_to_sheet

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "profile.json"
LOG_JSON_PATH = BASE_DIR / "applications_log.json"
LOG_CSV_PATH = BASE_DIR / "applications_log.csv"

# ---------------------------------------------------------------------------
# Phase 1: Robust, multi-ATS field selectors
# ---------------------------------------------------------------------------
# Each field maps to an ordered list of CSS selectors. Selectors are tried
# in order (most specific/ATS-specific first, generic fallbacks last) and
# the first one that matches a visible element wins. This covers common
# markup patterns used by Ashby, Greenhouse, Lever, and Workday.
FIELD_SELECTORS = {
    "full_name": [
        "input[name='_systemfield_name']",
        "input[aria-label='Legal Name' i]",
        "input[name='name']",
    ],
    "first_name": [
        # Ashby
        "input[name='_systemfield_name_first']",
        "input#first-name-input",
        # Greenhouse
        "input#first_name",
        "input[name='job_application[first_name]']",
        # Workday
        "input[data-automation-id='legalNameSection_firstName']",
        # Generic fallbacks
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

# ---------------------------------------------------------------------------
# Phase 3: Work authorization / sponsorship question detection
# ---------------------------------------------------------------------------
AUTHORIZATION_KEYWORDS = [
    "authorized to work",
    "legally authorized",
    "work authorization",
    "eligible to work",
]
SPONSORSHIP_KEYWORDS = [
    "require sponsorship",
    "require visa sponsorship",
    "need sponsorship",
    "sponsorship now or in the future",
]

YES_LABELS = ["yes", "true"]
NO_LABELS = ["no", "false"]


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Missing {PROFILE_PATH.name}. Copy profile.example.json to profile.json "
            "and fill in your details."
        )
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def try_selectors(page: Page, selectors: list[str]):
    """Return the first visible, enabled locator matching any selector, or None."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def fill_field(page: Page, field_name: str, value: str) -> bool:
    """Fill a text input using the fallback selector list for field_name."""
    if not value:
        return False
    selectors = FIELD_SELECTORS.get(field_name, [])
    locator = try_selectors(page, selectors)
    if locator is None:
        print(f"  [skip] No matching element found for '{field_name}'")
        return False
    try:
        locator.fill(str(value))
        print(f"  [ok] Filled '{field_name}'")
        return True
    except Exception as exc:
        print(f"  [error] Could not fill '{field_name}': {exc}")
        return False


# ---------------------------------------------------------------------------
# Phase 2: Resume / document upload handling
# ---------------------------------------------------------------------------
def handle_resume_upload(page: Page, profile: dict) -> bool:
    """Find a file input on the page and attach the resume from profile.json."""
    resume_path = profile.get("resume_path")
    if not resume_path:
        print("  [skip] No resume_path set in profile.json")
        return False

    resume_file = Path(resume_path)
    if not resume_file.is_absolute():
        resume_file = (BASE_DIR / resume_file).resolve()

    if not resume_file.exists():
        print(f"  [error] Resume file not found: {resume_file}")
        return False

    file_input = None
    for selector in FIELD_SELECTORS["resume"]:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                file_input = locator
                break
        except Exception:
            continue

    if file_input is None:
        print("  [skip] No file upload input found on page")
        return False

    try:
        file_input.set_input_files(str(resume_file))
        print(f"  [ok] Uploaded resume: {resume_file.name}")
        return True
    except Exception as exc:
        print(f"  [error] Resume upload failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Phase 3: Work authorization / sponsorship auto-answer
# ---------------------------------------------------------------------------
def _matches_any(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return any(keyword in text for keyword in keywords)


def _select_radio_or_checkbox(page: Page, question_container, want_yes: bool) -> bool:
    """Within a question's container, click the radio/label matching yes/no."""
    target_labels = YES_LABELS if want_yes else NO_LABELS
    options = question_container.locator("label, input[type='radio'], input[type='checkbox']")
    count = options.count()
    for i in range(count):
        option = options.nth(i)
        try:
            option_text = (option.inner_text() or "").strip().lower()
        except Exception:
            option_text = ""
        aria_label = (option.get_attribute("aria-label") or "").strip().lower()
        combined = f"{option_text} {aria_label}".strip()
        if not combined:
            continue
        if any(combined == label or combined.startswith(label) for label in target_labels):
            try:
                option.click()
                return True
            except Exception:
                continue
    return False


def _select_dropdown(page: Page, select_locator, want_yes: bool) -> bool:
    target_labels = YES_LABELS if want_yes else NO_LABELS
    try:
        select_options = select_locator.locator("option").all_inner_texts()
    except Exception:
        return False
    for label in target_labels:
        for option_text in select_options:
            if option_text.strip().lower().startswith(label):
                try:
                    select_locator.select_option(label=option_text)
                    return True
                except Exception:
                    continue
    return False


def _select_combobox_answer(page: Page, question: str, answer: str) -> bool:
    """Select an exact answer from a labeled ARIA combobox such as Greenhouse uses."""
    locator = page.get_by_role("combobox", name=re.compile(question, re.IGNORECASE)).first
    try:
        if locator.count() == 0 or not locator.is_visible():
            return False
        # A native <select> also has the combobox role but cannot be typed into.
        if locator.evaluate("e => e.tagName") == "SELECT":
            answer_re = re.compile(rf"^\s*{re.escape(answer)}(?:\s|$)", re.IGNORECASE)
            labels = locator.locator("option").all_inner_texts()
            match = next((label for label in labels if answer_re.search(label)), None)
            if match is None:
                print(f"  [skip] No '{answer}' option for '{question}'")
                return False
            locator.select_option(label=match)
            print(f"  [ok] Answered '{question}'")
            return True
        page.keyboard.press("Escape")
        locator.click()
        locator.fill(answer)
        option = page.get_by_role(
            "option", name=re.compile(rf"^{re.escape(answer)}(?:\s|$)", re.IGNORECASE)
        ).last
        option.wait_for(state="visible", timeout=2000)
        option.click()
        print(f"  [ok] Answered '{question}'")
        return True
    except Exception as exc:
        print(f"  [skip] Could not answer '{question}': {exc}")
        return False


def fill_known_profile_questions(page: Page, profile: dict) -> int:
    """Fill application answers supported directly by the applicant profile."""
    answered = 0
    for field_name in ("preferred_name", "linkedin_url", "portfolio_url"):
        if try_selectors(page, FIELD_SELECTORS[field_name]) is not None:
            answered += fill_field(page, field_name, profile.get(field_name, ""))

    years = profile.get("years_professional_software_engineering")
    if years is not None:
        answered += _select_combobox_answer(
            page, r"over 3 years.*professional software engineering", "Yes" if years > 3 else "No"
        )

    for profile_key, question in (
        ("ruby_on_rails_rating", r"rate yourself in Ruby on Rails"),
        ("python_rating", r"rate yourself in Python"),
    ):
        value = profile.get(profile_key)
        if value is not None:
            answered += _select_combobox_answer(page, question, str(value))

    country = profile.get("country")
    if country:
        answered += _select_combobox_answer(page, r"currently live in this location", "Yes")
        answered += _select_combobox_answer(page, r"current country of residence", country)

    restrictions = profile.get("employment_restrictions")
    if restrictions is not None:
        answered += _select_combobox_answer(
            page, r"employment agreements.*post-employment restrictions", "Yes" if restrictions else "No"
        )

    if "requires_sponsorship" in profile:
        answered += _select_combobox_answer(
            page, r"require sponsorship.*visa", "Yes" if profile["requires_sponsorship"] else "No"
        )

    previous_gitlab = profile.get("previously_worked_at_gitlab")
    if previous_gitlab is not None:
        answered += _select_combobox_answer(
            page, r"previously worked at or consulted for GitLab", "Yes" if previous_gitlab else "No"
        )

    print(f"  [ok] Filled {answered} additional profile question(s)")
    return answered


def fill_open_ended_fields(page: Page) -> int:
    """Draft and fill eligible blank prose fields for human review."""
    candidates = []
    fields = page.locator("textarea, input[type='text']")
    for index in range(min(fields.count(), 150)):
        field = fields.nth(index)
        try:
            if not field.is_visible() or field.input_value().strip():
                continue
            if (field.get_attribute("role") or "").lower() == "combobox":
                continue
            label = field.evaluate(
                """element => {
                    const labels = element.labels ? Array.from(element.labels) : [];
                    return labels.map(label => label.innerText.trim()).filter(Boolean).join(' | ')
                        || element.getAttribute('aria-label')
                        || element.getAttribute('placeholder')
                        || '';
                }"""
            )
            tag_name = field.evaluate("element => element.tagName.toLowerCase()")
            input_type = field.get_attribute("type") or tag_name
            if is_eligible_open_question(label, tag_name, input_type):
                candidates.append((field, label))
        except Exception:
            continue

    if not candidates:
        return 0
    try:
        page_context = f"{page.title()}\n{page.locator('body').inner_text()[:4000]}"
    except Exception:
        page_context = ""
    answers = draft_answers([label for _, label in candidates], page_context)
    filled = 0
    for (field, label), answer in zip(candidates, answers):
        if not answer:
            print(f"  [review] No grounded draft for: {label}")
            continue
        try:
            field.fill(answer)
            filled += 1
            print(f"  [ok] Drafted response for: {label}")
        except Exception as error:
            print(f"  [skip] Could not fill drafted response for '{label}': {error}")
    return filled


def handle_work_authorization(page: Page, profile: dict) -> int:
    """
    Scan the page for work-authorization / sponsorship questions and answer
    them using profile.json's authorized_to_work_us and requires_sponsorship.
    Returns the number of questions successfully answered.
    """
    authorized = bool(profile.get("authorized_to_work_us", False))
    needs_sponsorship = bool(profile.get("requires_sponsorship", False))
    answered = 0

    # Look for common question wrapper elements (fieldset, div, li) that
    # contain question text plus radio/select controls.
    question_blocks = page.locator(
        "fieldset, div:has(input[type='radio']), div:has(select), li:has(input[type='radio'])"
    )
    total = min(question_blocks.count(), 200)  # safety cap

    for i in range(total):
        block = question_blocks.nth(i)
        try:
            block_text = (block.inner_text() or "").strip()
        except Exception:
            continue
        if not block_text:
            continue

        if _matches_any(block_text, AUTHORIZATION_KEYWORDS):
            select_el = block.locator("select").first
            if select_el.count() > 0:
                if _select_dropdown(page, select_el, authorized):
                    answered += 1
                    continue
            if _select_radio_or_checkbox(page, block, authorized):
                answered += 1
                continue

        if _matches_any(block_text, SPONSORSHIP_KEYWORDS):
            select_el = block.locator("select").first
            if select_el.count() > 0:
                if _select_dropdown(page, select_el, needs_sponsorship):
                    answered += 1
                    continue
            if _select_radio_or_checkbox(page, block, needs_sponsorship):
                answered += 1
                continue

    print(f"  [ok] Answered {answered} work authorization/sponsorship question(s)")
    return answered


# ---------------------------------------------------------------------------
# Phase 4: Local state & logging
# ---------------------------------------------------------------------------
def parse_company_name(page: Page, url: str) -> str:
    """Best-effort company name extraction from the page title, else the URL host."""
    try:
        title = page.title().strip()
    except Exception:
        title = ""

    if title:
        # Titles are often "Job Title at Company" or "Company - Job Title"
        for sep in (" at ", " @ ", " - ", " | "):
            if sep in title:
                parts = title.split(sep)
                candidate = parts[-1].strip() if sep == " at " or sep == " @ " else parts[0].strip()
                if candidate:
                    return candidate
        return title

    host = urlparse(url).netloc
    return host.replace("www.", "").split(".")[0].capitalize()


def log_application(url: str, company: str, status: str = "Pending Review") -> None:
    """Append a record to applications_log.json and applications_log.csv."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "company": company,
        "status": status,
    }

    # JSON log
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

    # CSV log
    write_header = not LOG_CSV_PATH.exists()
    with open(LOG_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "url", "company", "status"])
        if write_header:
            writer.writeheader()
        writer.writerow(record)

    print(f"  [log] {company} | {status} -> {LOG_JSON_PATH.name}, {LOG_CSV_PATH.name}")

    try:
        sync_logs_to_sheet()
    except Exception as exc:
        print(f"  [warn] Google Sheet sync skipped: {exc}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bot-wall / CAPTCHA handling
# ---------------------------------------------------------------------------
# Signals that a Cloudflare challenge, Turnstile, reCAPTCHA, or hCaptcha is
# blocking the page. We never attempt to auto-solve these -- solving them
# programmatically is exactly the anti-automation control most sites intend
# to enforce, and doing so risks violating the target site's terms of
# service. Instead we detect the challenge and pause for a human to solve it
# in the visible (headed) browser window, since a person is already there to
# review the application before submission.
def detect_bot_challenge(page: Page) -> bool:
    """
    Return True if a challenge is blocking the page (see bot_challenge.py).
    The invisible reCAPTCHA badge and in-form checkboxes do not count.
    """
    try:
        return bool(page.evaluate(BLOCKING_CHALLENGE_JS))
    except Exception:
        return False


def wait_for_human_to_clear_challenge(page: Page, max_checks: int = 1) -> None:
    """Notify the user and block on input() until they've solved a bot challenge."""
    print("\n  [!] Cloudflare/CAPTCHA challenge detected in the browser window.")
    input("      Please solve it manually, then press Enter here to continue... ")
    for _ in range(max_checks):
        if not detect_bot_challenge(page):
            return
        input("      Challenge still appears active. Solve it, then press Enter again... ")


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


def _page_is_dead_posting(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text()
    except Exception:
        return False
    return bool(DEAD_POSTING_RE.search(body or ""))


def _click_apply_control(page: Page) -> bool:
    """Click the first genuine apply link or button. Returns True if one was clicked."""
    for role in ("link", "button"):
        controls = page.get_by_role(role, name=APPLY_TEXT_RE)
        for index in range(min(controls.count(), 10)):
            control = controls.nth(index)
            if not control.is_visible():
                continue
            try:
                text = control.inner_text() or ""
            except Exception:
                text = ""
            if APPLY_REJECT_RE.search(text):
                continue
            if role == "link":
                href = control.get_attribute("href") or ""
                if not href or href.rstrip("/").endswith("#"):
                    continue
            control.click()
            page.wait_for_load_state("domcontentloaded")
            return True
    return False


def open_application_form(page: Page) -> bool:
    """Open an application tab when an ATS initially shows only the job overview."""
    if try_selectors(page, FIELD_SELECTORS["email"]) is not None:
        return False

    embed_url = find_embedded_form_url(page)
    if embed_url:
        page.goto(embed_url, wait_until="domcontentloaded")
        print("  [ok] Opened embedded application form")
        return True

    if _page_is_dead_posting(page):
        raise PermanentFailure("Posting is no longer available (page not found/closed)")

    if _click_apply_control(page):
        page.wait_for_timeout(1500)
        embed_url = find_embedded_form_url(page)
        if embed_url:
            page.goto(embed_url, wait_until="domcontentloaded")
            print("  [ok] Opened embedded application form")
            return True
        if try_selectors(page, FIELD_SELECTORS["email"]) is not None:
            print("  [ok] Opened application form")
            return True

    for role in ("tab", "link", "button"):
        locator = page.get_by_role(role, name=re.compile(r"^Application$", re.IGNORECASE)).first
        try:
            if locator.count() > 0 and locator.is_visible():
                locator.click()
                page.wait_for_timeout(500)
                print("  [ok] Opened application form")
                return True
        except Exception:
            continue

    locator = page.get_by_text("Application", exact=True).first
    try:
        if locator.count() > 0 and locator.is_visible():
            locator.click()
            page.wait_for_timeout(500)
            print("  [ok] Opened application form")
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Self-healing execution wrapper (Gemini-assisted diagnosis)
# ---------------------------------------------------------------------------
FAILURE_SNAPSHOT_DIR = BASE_DIR / "failures"
SELF_HEAL_LOG_PATH = BASE_DIR / "self_heal_log.json"
SELF_HEAL_MODEL = "gemini-2.5-flash"


def _log_self_heal_event(action_name: str, url: str, error: str, suggestion: str) -> None:
    """Append a diagnosis record to self_heal_log.json so past adaptations are kept for review."""
    records = []
    if SELF_HEAL_LOG_PATH.exists():
        try:
            with open(SELF_HEAL_LOG_PATH, "r", encoding="utf-8") as f:
                records = json.load(f)
        except (json.JSONDecodeError, ValueError):
            records = []
    records.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action_name,
        "url": url,
        "error": error,
        "gemini_suggestion": suggestion,
    })
    with open(SELF_HEAL_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def _diagnose_failure(page: Page, action_name: str, error: Exception) -> str:
    """
    Capture a screenshot + visible page text and ask Gemini for a plain-language
    recovery suggestion.

    This is advisory only: the suggestion is printed for a human to read, never
    executed. Page content is untrusted third-party input, so treating a model's
    response to it as executable code would be an arbitrary-code-execution risk
    (e.g. via prompt injection hidden in the page). The human already reviews
    the browser before submission, so they act on the suggestion themselves.
    """
    FAILURE_SNAPSHOT_DIR.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", action_name)[:60]
    screenshot_path = FAILURE_SNAPSHOT_DIR / f"{safe_name}_{int(datetime.now().timestamp())}.png"
    try:
        page.screenshot(path=str(screenshot_path))
    except Exception:
        screenshot_path = None

    try:
        page_text = page.evaluate("() => document.body.innerText.slice(0, 3000)")
    except Exception:
        page_text = "(could not read page text)"

    prompt = (
        "You are helping diagnose a failed browser-automation step on a job "
        "application form. Do not return executable code -- only a short, "
        "plain-language description of what element, label, or selector the "
        "human operator should look for or click next.\n\n"
        f"Goal: {action_name}\n"
        f"Error encountered: {error}\n"
        f"Visible page text (truncated):\n{page_text}\n"
    )

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        suggestion = "(Gemini diagnosis unavailable: GEMINI_API_KEY is not set in this shell.)"
    else:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(model=SELF_HEAL_MODEL, contents=prompt)
            suggestion = (response.text or "").strip()
        except Exception as ai_err:
            suggestion = f"(Gemini diagnosis unavailable: {ai_err})"

    if screenshot_path is not None:
        print(f"  [self-heal] Screenshot saved: {screenshot_path.relative_to(BASE_DIR)}")
    print(f"  [self-heal] Gemini suggestion:\n    {suggestion}\n")
    return suggestion


class PermanentFailure(Exception):
    """
    A failure retrying cannot fix - a pulled posting, a closed req.

    self_heal_action re-raises these immediately instead of burning retries
    and a Gemini diagnosis call on a page that will never change.
    """


def self_heal_action(page: Page, action_name: str, action_fn, url: str = "", max_retries: int = 3):
    """
    Run action_fn(page) inside a retry loop. On failure, capture a screenshot +
    page text, ask Gemini for a human-readable diagnosis, log the event to
    self_heal_log.json, and retry (the page may have changed, or a human may
    have intervened in the visible browser). On the final failure, pause for
    manual intervention instead of crashing the run.
    """
    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [self-heal] Attempt {attempt}/{max_retries}: {action_name}")
            return action_fn(page)
        except PermanentFailure:
            raise
        except Exception as exc:
            print(f"  [self-heal] '{action_name}' failed: {exc}")

            if attempt == max_retries:
                print(f"  [self-heal] Max retries reached for '{action_name}'.")
                _diagnose_failure(page, action_name, exc)
                input("      Please resolve it manually in the browser, then press Enter to continue... ")
                return None

            suggestion = _diagnose_failure(page, action_name, exc)
            _log_self_heal_event(action_name, url, str(exc), suggestion)
            time.sleep(2)

    return None



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



def _checkbox_group_key(checkbox) -> str:
    """Identify the question group a checkbox belongs to (name attr, else fieldset)."""
    name = checkbox.get_attribute("name")
    if name:
        return f"name:{name}"
    legend = checkbox.evaluate(
        "e => { const f = e.closest('fieldset,[role=group]');"
        " return f ? (f.querySelector('legend')?.textContent || 'group').trim() : ''; }"
    )
    return f"group:{legend}" if legend else ""


def _checkbox_is_required(checkbox) -> bool:
    """True only if the markup itself marks this checkbox as required."""
    if checkbox.get_attribute("required") is not None:
        return True
    return (checkbox.get_attribute("aria-required") or "").lower() == "true"


def _checkbox_label_text(page: Page, checkbox) -> str:
    """Best-effort label text for a checkbox, from aria-label, <label for>, or parent."""
    aria = checkbox.get_attribute("aria-label")
    if aria:
        return aria
    cb_id = checkbox.get_attribute("id")
    if cb_id:
        label = page.locator(f"label[for='{cb_id}']").first
        if label.count():
            try:
                return label.inner_text()
            except Exception:
                pass
    try:
        return checkbox.locator("xpath=..").inner_text()
    except Exception:
        return ""


def check_required_checkboxes(page: Page) -> int:
    """
    Tick only checkboxes the form marks required, excluding self-identification
    and personal attestations. Returns the number actually checked.
    """
    checked = 0
    checkboxes = page.locator("input[type='checkbox']")
    total = min(checkboxes.count(), 100)

    group_sizes: dict[str, int] = {}
    for index in range(total):
        box = checkboxes.nth(index)
        if not box.is_visible():
            continue
        key = _checkbox_group_key(box)
        if key:
            group_sizes[key] = group_sizes.get(key, 0) + 1

    # Groups that already have a selection were answered elsewhere (e.g.
    # select_work_countries) and must not be re-reported as outstanding.
    satisfied_groups: set[str] = set()
    for index in range(total):
        box = checkboxes.nth(index)
        if not box.is_visible() or not box.is_checked():
            continue
        key = _checkbox_group_key(box)
        if key:
            satisfied_groups.add(key)

    reported_groups: set[str] = set()
    for index in range(total):
        box = checkboxes.nth(index)
        if not box.is_visible() or box.is_checked():
            continue
        if not _checkbox_is_required(box):
            continue

        key = _checkbox_group_key(box)
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

        label = _checkbox_label_text(page, box)
        verdict = _classify_checkbox(label)
        if verdict != "ok":
            print(f"  [review] Required checkbox left for you ({verdict}): {_summarize(label)}")
            continue

        box.check()
        checked += 1
        print(f"  [ok] Checked required box: {_summarize(label)}")

    if checked:
        print(f"  [ok] Checked {checked} required checkbox(es)")
    return checked


def report_multi_step_form(page: Page) -> bool:
    """Detect (never click) a pagination control and report it at the review gate."""
    for selector in PAGINATION_BUTTON_SELECTORS:
        button = page.locator(selector).first
        if button.count() and button.is_visible():
            try:
                label = _summarize(button.inner_text(), 24)
            except Exception:
                label = selector
            print(f"  [review] Multi-step form: a '{label}' step follows - not clicked.")
            return True
    return False


def fill_location_field(page: Page, profile: dict) -> bool:
    """Fill a location/city field from profile['location_city'].

    See the async twin in batch_runner.py for why options are scoped to this
    widget's listbox and why blind keyboard selection is avoided.
    """
    city = profile.get("location_city", "")
    if not city:
        return False
    field = None
    for selector in FIELD_SELECTORS["location"]:
        candidates = page.locator(selector)
        for index in range(min(candidates.count(), 10)):
            candidate = candidates.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                label = candidate.evaluate(
                    "e => (e.getAttribute('aria-label') || (e.id && document.querySelector("
                    "'label[for=\"' + e.id + '\"]')?.innerText) || '').trim()"
                )
            except Exception:
                continue
            # Skip question fields that merely mention "location", e.g. "If you're
            # not authorized to work at the stated location, what sponsorship...?"
            if label and (
                len(label) > 60
                or SPONSORSHIP_QUESTION_RE.search(label)
                or AUTHORIZATION_QUESTION_RE.search(label)
            ):
                continue
            field = candidate
            break
        if field is not None:
            break
    if field is None:
        return False

    field.click()
    field.fill(city)

    listbox_id = field.get_attribute("aria-controls")
    options = (
        page.locator(f"[id='{listbox_id}'] [role='option']")
        if listbox_id
        else page.get_by_role("option")
    )
    try:
        options.first.wait_for(state="visible", timeout=5000)
    except Exception:
        options = None

    if options is not None and options.count():
        head = city.split(",")[0].strip().lower()
        chosen = options.first
        for index in range(min(options.count(), 10)):
            candidate = options.nth(index)
            try:
                text = (candidate.inner_text() or "").strip().lower()
            except Exception:
                continue
            if text.startswith(head):
                chosen = candidate
                break
        chosen.click()

    # react-select clears its search input on selection and renders the chosen
    # value in a sibling node, so input_value() is empty even on success.
    try:
        final = field.evaluate("""e => { const c = e.closest('[class*=control]') || e.parentElement; return ((c && c.innerText) || '').trim(); }""") or ""
    except Exception:
        final = ""
    if not final:
        try:
            final = field.input_value().strip()
        except Exception:
            final = ""
    if not final:
        print("  [warn] Location field did not retain a value; left for review")
        return False
    print(f"  [ok] Filled 'location' with {_summarize(final, 40)}")
    return True


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


def _field_is_empty(field) -> bool:
    """True if a required field still has no value."""
    tag = (field.evaluate("e => e.tagName.toLowerCase()")) or ""
    if tag == "select":
        return not field.evaluate("e => e.value")
    field_type = (field.get_attribute("type") or "").lower()
    if field_type == "file":
        return not field.evaluate("e => e.files && e.files.length > 0")
    if field_type in ("checkbox", "radio"):
        return False
    try:
        if field.input_value().strip():
            return False
    except Exception:
        return False
    # Only a combobox hides its value outside the input. For a plain text input
    # an empty input_value() means empty - falling back to container text here
    # picks up the field's own LABEL and silently reports it as filled.
    if (field.get_attribute("role")) != "combobox":
        return True
    try:
        rendered = field.evaluate("""e => { const c = e.closest('[class*=control]') || e.parentElement; return ((c && c.innerText) || '').trim(); }""") or ""
    except Exception:
        rendered = ""
    normalized = " ".join(rendered.split())
    if not normalized:
        return True
    # An unselected combobox control renders only its placeholder.
    return bool(PLACEHOLDER_LABEL_RE.match(normalized))


def report_unfilled_required_fields(page: Page) -> list[str]:
    """List required fields still empty, so the review gate names them."""
    unfilled: list[str] = []
    fields = page.locator(REQUIRED_FIELD_SELECTOR)
    for index in range(min(fields.count(), 120)):
        field = fields.nth(index)
        if not field.is_visible() or not _field_is_empty(field):
            continue
        label = (_checkbox_label_text(page, field)) or ""
        label = " ".join(label.split())
        if PLACEHOLDER_LABEL_RE.match(label):
            continue  # the visible half of a combobox already reported by label
        entry = _summarize(label or (field.get_attribute("name")) or "unnamed field")
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
COMBOBOX_LABEL_JS = r"""e => {
  let n = e, label = '';
  for (let k = 0; k < 6 && n; k++) {
    n = n.parentElement;
    if (n && n.innerText && n.innerText.trim().length > 12) { label = n.innerText.trim(); break; }
  }
  return label.split('\n')[0];
}"""


def answer_labeled_combobox(
    page: Page, label_re, answers: list[str] | None = None, option_re=None,
    search_text: str = "", exclude_re=None, all_matches: bool = False,
) -> bool:
    """Answer a custom combobox whose nearby label matches label_re.

    See the async twin in batch_runner.py for the scoping rationale.
    """
    answered_any = False
    boxes = page.locator("input[role='combobox']")
    for index in range(min(boxes.count(), 40)):
        box = boxes.nth(index)
        if not box.is_visible():
            continue
        try:
            label = box.evaluate(COMBOBOX_LABEL_JS)
        except Exception:
            continue
        if not label or not label_re.search(label):
            continue
        # A question can match more than one topic - Elastic asks "Will you
        # require Elastic's sponsorship to continue or extend your work
        # authorization status?", which reads as both. exclude_re lets the
        # more specific handler claim it so the wrong answer is never given.
        if exclude_re is not None and exclude_re.search(label):
            continue

        box.click()
        if search_text:
            # Typeahead lists (School) only render options once filtered.
            box.fill(search_text)
            page.wait_for_timeout(1200)
        listbox_id = box.get_attribute("aria-controls")
        options = (
            page.locator(f"[id='{listbox_id}'] [role='option']")
            if listbox_id
            else page.get_by_role("option")
        )
        try:
            options.first.wait_for(state="visible", timeout=5000)
        except Exception:
            page.keyboard.press("Escape")
            continue

        # Snapshot option text once: clicking closes the menu, so any further
        # click attempt against this list would hang until timeout.
        texts: list[str] = []
        for opt_index in range(min(options.count(), 40)):
            try:
                texts.append((options.nth(opt_index).inner_text() or "").strip())
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
            options.nth(chosen_index).click()
            print(f"  [ok] Answered '{_summarize(label, 46)}' -> {_summarize(texts[chosen_index], 30)}")
            if not all_matches:
                return True
            answered_any = True
            continue  # menu is closed; move to the next control

        page.keyboard.press("Escape")
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
]
DECLINE_OPTION_RE = re.compile(
    r"(don'?t\s+wish\s+to\s+(answer|disclose)|do\s+not\s+wish\s+to\s+(answer|disclose)|"
    r"decline\s+to\s+(self[-\s]?identify|answer|disclose)|prefer\s+not\s+to\s+(say|answer|disclose)|"
    r"choose\s+not\s+to\s+(disclose|answer)|i\s+do\s+not\s+wish)",
    re.IGNORECASE,
)
SCHOOL_QUESTION_RE = re.compile(r"\bschool\b|\buniversity\b|\bcollege\b", re.IGNORECASE)
DEGREE_QUESTION_RE = re.compile(r"\bdegree\b", re.IGNORECASE)


def answer_eeo_comboboxes(page: Page, profile: dict) -> int:
    """Select the form's own decline option on EEO questions, if opted in."""
    if str(profile.get("eeo_response", "")).lower() != "decline":
        return 0
    answered = 0
    for question_re in EEO_QUESTION_RES:
        if answer_labeled_combobox(page, question_re, option_re=DECLINE_OPTION_RE):
            answered += 1
    if answered:
        print(f"  [ok] Declined to self-identify on {answered} EEO question(s)")
    return answered


def fill_education_fields(page: Page, profile: dict) -> int:
    """Fill School/Degree from profile.json without inventing credentials."""
    filled = 0
    school_candidates = [profile.get("school", "")] + list(profile.get("school_fallbacks", []))
    for candidate in [c for c in school_candidates if c]:
        if answer_labeled_combobox(page, SCHOOL_QUESTION_RE, answers=[candidate], search_text=candidate):
            filled += 1
            break

    degrees = list(profile.get("degree_preferences", []))
    if degrees and answer_labeled_combobox(page, DEGREE_QUESTION_RE, answers=degrees):
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
    r"country\s+(where\s+you\s+currently\s+reside|of\s+residence)|current\s+country", re.IGNORECASE
)
PRIOR_EMPLOYMENT_RE = re.compile(
    r"(ever|previously)\s+(been\s+)?employed\s+(by|at|with)|previously\s+worked\s+(at|for)|former\s+employee",
    re.IGNORECASE,
)
REMOTE_PLAN_RE = re.compile(
    r"(plan|intend)\s+to\s+work\s+remotely|work\s+from\s+a\s+remote\s+location", re.IGNORECASE
)


def answer_logistics_questions(page: Page, profile: dict) -> int:
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
        if answer_labeled_combobox(page, COUNTRY_QUESTION_RE, answers=ordered):
            answered += 1

    if "previously_employed_at_target" in profile:
        want = "Yes" if profile["previously_employed_at_target"] else "No"
        if answer_labeled_combobox(page, PRIOR_EMPLOYMENT_RE, answers=[want]):
            answered += 1


    if "recruiting_messages_opt_in" in profile:
        want = "Yes" if profile["recruiting_messages_opt_in"] else "No"
        if answer_labeled_combobox(page, RECRUITING_OPTIN_RE, answers=[want]):
            answered += 1
    if "plans_to_work_remotely" in profile:
        want = "Yes" if profile["plans_to_work_remotely"] else "No"
        if answer_labeled_combobox(page, REMOTE_PLAN_RE, answers=[want]):
            answered += 1

    return answered

# Short free-text questions answerable straight from profile.json.
TEXT_QUESTION_RULES = [
    (re.compile(r"current\s+or\s+previous\s+job\s+title|current\s+job\s+title|"
                r"most\s+recent\s+job\s+title", re.IGNORECASE), "current_job_title"),
    (re.compile(r"current\s+or\s+previous\s+employer|current\s+employer|"
                r"most\s+recent\s+(company|employer)|name\s+of\s+your\s+current", re.IGNORECASE),
     "current_employer"),
    (re.compile(r"city\s+and\s+state|what\s+city.*reside|city/state", re.IGNORECASE), "city_state"),
]
# "Which countries will you work in?" is a required multi-select; the answer is
# a fact from profile.json rather than a consent tick, so it can be answered.
WORK_COUNTRIES_RE = re.compile(
    r"countr(y|ies)\s+you\s+anticipate\s+working|countries?\s+you\s+(will|plan\s+to)\s+work",
    re.IGNORECASE,
)


def fill_profile_text_questions(page: Page, profile: dict) -> int:
    """Fill short free-text questions whose answer lives in profile.json."""
    filled = 0
    inputs = page.locator("input[type='text'], input:not([type])")
    for index in range(min(inputs.count(), 120)):
        field = inputs.nth(index)
        if not field.is_visible() or field.get_attribute("role") == "combobox":
            continue
        try:
            if field.input_value().strip():
                continue
        except Exception:
            continue
        label = " ".join((_checkbox_label_text(page, field) or "").split())
        if not label:
            continue
        for pattern, key in TEXT_QUESTION_RULES:
            value = profile.get(key, "")
            if value and pattern.search(label):
                field.fill(str(value))
                print(f"  [ok] Answered '{_summarize(label, 44)}' -> {_summarize(str(value), 30)}")
                filled += 1
                break
    return filled


def select_work_countries(page: Page, profile: dict) -> int:
    """Tick the work-country checkboxes named in profile['work_countries']."""
    wanted = [str(c).strip().lower() for c in profile.get("work_countries", []) if str(c).strip()]
    if not wanted:
        return 0
    checked = 0
    boxes = page.locator("input[type='checkbox']")
    for index in range(min(boxes.count(), 120)):
        box = boxes.nth(index)
        if not box.is_visible() or box.is_checked():
            continue
        try:
            group_label = box.evaluate(
                "e => { const f = e.closest('fieldset,[role=group]');"
                " return f ? (f.querySelector('legend')?.textContent || '') : ''; }"
            )
        except Exception:
            group_label = ""
        if not group_label or not WORK_COUNTRIES_RE.search(group_label):
            continue
        label = " ".join((_checkbox_label_text(page, box) or "").split()).lower()
        if label in wanted:
            box.check()
            checked += 1
            print(f"  [ok] Work country selected: {label.upper()}")
    return checked

def handle_authorization_comboboxes(page: Page, profile: dict) -> int:
    """Answer work-authorization and sponsorship comboboxes from profile.json."""
    answered = 0
    authorized = bool(profile.get("authorized_to_work_us", False))
    sponsorship = bool(profile.get("requires_sponsorship", False))

    # Sponsorship is checked first and authorization explicitly skips any
    # sponsorship-worded question: answering "Yes, I am authorized" into a
    # "do you require sponsorship?" control is a materially wrong answer.
    if answer_labeled_combobox(
        page, SPONSORSHIP_QUESTION_RE, ["Yes"] if sponsorship else ["No"],
        all_matches=True,
    ):
        answered += 1
    if answer_labeled_combobox(
        page, AUTHORIZATION_QUESTION_RE, ["Yes"] if authorized else ["No"],
        exclude_re=SPONSORSHIP_QUESTION_RE, all_matches=True,
    ):
        answered += 1
    return answered



def repair_cleared_fields(page: Page, profile: dict) -> int:
    """Re-fill contact/text fields that a later step wiped. See async twin."""
    email = try_selectors(page, FIELD_SELECTORS["email"])
    needs_repair = False
    if email is not None:
        try:
            needs_repair = not email.input_value().strip()
        except Exception:
            needs_repair = False
    if not needs_repair:
        for key in ("full_name", "first_name"):
            field = try_selectors(page, FIELD_SELECTORS[key])
            if field is None:
                continue
            try:
                if not field.input_value().strip():
                    needs_repair = True
            except Exception:
                pass
            break

    if not needs_repair:
        return 0
    print("  [repair] Contact fields were cleared by a later step; re-filling.")
    full_name = " ".join(
        part for part in (profile.get("first_name", ""), profile.get("last_name", "")) if part
    )
    if not fill_field(page, "full_name", full_name):
        fill_field(page, "first_name", profile.get("first_name", ""))
        fill_field(page, "last_name", profile.get("last_name", ""))
    fill_field(page, "email", profile.get("email", ""))
    fill_field(page, "phone", profile.get("phone", ""))
    fill_profile_text_questions(page, profile)
    return 1


def apply_to_job(page: Page, url: str, profile: dict, pause_for_review: bool = True) -> None:
    print(f"\n=== Processing: {url} ===")
    page.goto(url, wait_until="domcontentloaded")

    if detect_bot_challenge(page):
        wait_for_human_to_clear_challenge(page)

    open_application_form(page)
    # Server-rendered forms (Greenhouse) show inputs before their JavaScript
    # loads; a résumé set in that window never uploads. See batch_runner.py.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    def _fill_contact_fields(p: Page) -> bool:
        full_name = " ".join(
            part for part in (profile.get("first_name", ""), profile.get("last_name", "")) if part
        )
        name_ok = fill_field(p, "full_name", full_name)
        if not name_ok:
            first_ok = fill_field(p, "first_name", profile.get("first_name", ""))
            last_ok = fill_field(p, "last_name", profile.get("last_name", ""))
            name_ok = first_ok and last_ok
        email_ok = fill_field(p, "email", profile.get("email", ""))
        fill_field(p, "phone", profile.get("phone", ""))
        if not (name_ok and email_ok):
            raise RuntimeError("One or more required contact fields could not be located")
        return True

    self_heal_action(page, "Fill contact fields", _fill_contact_fields, url=url)
    self_heal_action(page, "Upload resume", lambda p: handle_resume_upload(p, profile), url=url)
    self_heal_action(page, "Fill profile questions", lambda p: fill_known_profile_questions(p, profile), url=url)
    self_heal_action(page, "Draft open-ended responses", fill_open_ended_fields, url=url)
    self_heal_action(
        page, "Answer work authorization questions", lambda p: handle_work_authorization(p, profile), url=url
    )
    self_heal_action(page, "Fill location", lambda p: fill_location_field(p, profile), url=url)
    self_heal_action(
        page, "Answer authorization questions",
        lambda p: handle_authorization_comboboxes(p, profile), url=url
    )
    self_heal_action(page, "Fill education", lambda p: fill_education_fields(p, profile), url=url)
    self_heal_action(
        page, "Answer logistics questions",
        lambda p: answer_logistics_questions(p, profile), url=url
    )
    self_heal_action(
        page, "Fill profile text questions",
        lambda p: fill_profile_text_questions(p, profile), url=url
    )
    self_heal_action(page, "Select work countries", lambda p: select_work_countries(p, profile), url=url)
    self_heal_action(page, "EEO self-identification", lambda p: answer_eeo_comboboxes(p, profile), url=url)
    self_heal_action(page, "Check required checkboxes", lambda p: check_required_checkboxes(p), url=url)
    self_heal_action(page, "Repair cleared fields", lambda p: repair_cleared_fields(p, profile), url=url)
    report_multi_step_form(page)
    report_unfilled_required_fields(page)

    company = parse_company_name(page, url)
    log_application(url, company, status="Pending Review")

    if pause_for_review:
        print("  Form pre-filled. Pausing for manual review/submission...")
        page.pause()
    else:
        print("  Form pre-filled and left open for manual review.")


def run_application_agent(url: str, skip_if_handled: bool = True) -> None:
    """Open a single job application URL and pre-fill it (used by cli.py's `apply`)."""
    url = url.strip().rstrip("~")
    if skip_if_handled and url in load_handled_urls():
        print(f"  [dedup] Already prepared in a previous run, skipping: {url}")
        return
    profile = load_profile()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        if Stealth is not None:
            Stealth().apply_stealth_sync(context)
        else:
            print("  [warn] playwright-stealth not installed; running without stealth patches.")
        page = context.new_page()
        try:
            apply_to_job(page, url, profile)
        except Exception as exc:
            print(f"  [error] Failed processing {url}: {exc}")
            company = parse_company_name(page, url)
            log_application(url, company, status=f"Error: {exc}")
        finally:
            browser.close()


def run_application_review_batch(jobs: list[dict]) -> None:
    """Prepare each selected job in its own tab, then pause once for human review."""
    profile = load_profile()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        if Stealth is not None:
            Stealth().apply_stealth_sync(context)
        else:
            print("  [warn] playwright-stealth not installed; running without stealth patches.")

        prepared_pages = []
        try:
            for index, job in enumerate(jobs, start=1):
                url = job["url"].strip().rstrip("~")
                title = job.get("title", url)
                print(f"\n=== [{index}/{len(jobs)}] Preparing: {title} ===")
                page = context.new_page()
                try:
                    apply_to_job(page, url, profile, pause_for_review=False)
                    prepared_pages.append(page)
                except Exception as exc:
                    print(f"  [error] Failed preparing {title}: {exc}")
                    page.close()

            if not prepared_pages:
                raise RuntimeError("No applications were prepared for review")

            prepared_pages[-1].bring_to_front()
            print(f"\nPrepared {len(prepared_pages)} application(s) in browser tabs.")
            print("Review each tab and submit manually. Resume Playwright when finished.")
            prepared_pages[-1].pause()
        finally:
            browser.close()


def main():
    from cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
