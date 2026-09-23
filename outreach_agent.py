"""
Value-first outbound outreach agent for founders/CTOs/VPs of Engineering.

This is NOT a job-application filler. It never opens an application form and
never clicks anything resembling a send/submit control - see
`SEND_BUTTON_SELECTORS` and `report_send_controls`, which only ever detect and
report those controls. Every prepared tab is left open for a human to read
and click send themselves.

Pipeline per target company/contact:
    1. Load a target from a local JSON queue file (see --targets). Discovery
       of NEW targets (scraping YC/seed directories, etc.) is expected to
       populate this same JSON shape and is intentionally kept out of this
       module - see scraper.py for the equivalent job-board pattern.
    2. Visit the company's public blog/careers/website pages with Playwright
       and extract tech-stack signals (Python, FastAPI, Docker, Postgres, ...).
    3. Draft an ultra-concise, technical, value-first cold message with
       Gemini 2.5 Flash, grounded in candidate_context.md.
    4. Open the outreach channel (LinkedIn profile, Gmail compose, or a
       contact form) in a browser tab, fill in the draft, and STOP.
    5. Log every state transition (discovered/drafted/reviewed/sent) to a
       local SQLite database and optionally sync it to the shared tracker
       Google Sheet via sheets_sync.py.

Separate campaigns must not share a database: pass a distinct --output per
run, exactly like scraper.py's --output keeps separate job searches apart.

Run:
    python outreach_agent.py run --targets queues/outreach_targets.json \\
        --output queues/outreach_log.db
    python outreach_agent.py run --targets queues/outreach_targets.json \\
        --user-data-dir ~/.job-agent-browser-profile
    python outreach_agent.py mark-sent --output queues/outreach_log.db \\
        --company "Acme Inc" --contact "Jane Doe"
    python outreach_agent.py sync-sheet --output queues/outreach_log.db

Login walls: LinkedIn and X require an authenticated session to show a real
message composer. By default this agent launches a PERSISTENT Chromium
profile at --user-data-dir (~/.job-agent-browser-profile) so a session you
log into once carries over between runs. If that profile is already locked by
another running browser, it falls back to a fresh, logged-out context with a
warning. Either way, if a page still shows a login/signup wall the runner
never guesses credentials - it logs "auth_required" and leaves the tab open
for you to log in and send manually.

Target JSON shape (a list of these). Two forms are accepted:

  Full form - a named contact with an explicit channel:
    {
      "company": "Acme Inc",
      "website": "https://acme.example.com",
      "blog_url": "https://acme.example.com/blog",
      "careers_url": "https://acme.example.com/careers",
      "contacts": [
        {
          "name": "Jane Doe",
          "title": "CTO",
          "channel": "linkedin",            # "linkedin" | "x" | "email" | "contact_form"
          "profile_url": "https://www.linkedin.com/in/janedoe",
          "email": "jane@acme.example.com",
          "contact_form_url": "https://acme.example.com/contact"
        }
      ]
    }

  Flat form - one profile link, no named contact yet. normalize_target_record()
  infers the channel from contact_url's domain and any role hint (cto/founder/
  vp-eng) from the URL text itself, and turns a comma-separated tech_stack
  string into known_tech_stack (skipping the live scrape in step 2 below):
    {
      "company": "Acme Inc",
      "website": "https://acme.example.com",
      "contact_url": "https://www.linkedin.com/in/jane-cto-acme",
      "tech_stack": "Python, FastAPI, PostgreSQL, Docker, AWS",
      "context": "Early-stage backend scaling out a data pipeline."
    }
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from candidate_answers import load_candidate_context

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGETS_PATH = BASE_DIR / "queues" / "outreach_targets.json"
DEFAULT_DB_PATH = BASE_DIR / "queues" / "outreach_log.db"
# A persistent profile keeps LinkedIn/X/Gmail sessions logged in between runs,
# so the agent hits real composers instead of a fresh, unauthenticated login wall.
DEFAULT_USER_DATA_DIR = Path.home() / ".job-agent-browser-profile"
GEMINI_MODEL = "gemini-2.5-flash"

# Outreach state machine. "sent" is never written by the runner itself - only
# a human confirming through `mark-sent` after they click send themselves.
STATE_DISCOVERED = "discovered"
STATE_DRAFTED = "drafted"
STATE_REVIEWED = "reviewed"
STATE_SENT = "sent"
STATE_ERROR = "error"
# The page redirected to (or already shows) a login/signup wall - filling was
# skipped rather than attempted against login-page markup.
STATE_AUTH_REQUIRED = "auth_required"
# The page is authenticated (or public) but no fillable composer was found -
# e.g. a private profile, or a layout this agent's selectors don't cover yet.
STATE_NO_COMPOSER_FOUND = "no_composer_found"
VALID_STATES = {
    STATE_DISCOVERED, STATE_DRAFTED, STATE_REVIEWED, STATE_SENT, STATE_ERROR,
    STATE_AUTH_REQUIRED, STATE_NO_COMPOSER_FOUND,
}

# Enforced between successive per-target actions (page loads, Gemini calls)
# so a large target list cannot hammer company sites, LinkedIn, or the Gemini
# API back-to-back.
DEFAULT_MIN_INTERVAL_SECONDS = 4.0

TECH_SIGNAL_KEYWORDS = [
    "Python", "FastAPI", "Django", "Flask", "Docker", "Kubernetes", "Postgres",
    "PostgreSQL", "MySQL", "Redis", "Kafka", "gRPC", "GraphQL", "React", "TypeScript",
    "Node.js", "Go", "Rust", "AWS", "GCP", "Azure", "Terraform", "Airflow",
    "data pipeline", "microservices", "event-driven", "CI/CD", "Playwright",
]
TECH_SIGNAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(keyword) for keyword in TECH_SIGNAL_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Best-effort composer/field selectors across LinkedIn's message composer, X's
# DM composer, Gmail's compose window, and generic contact forms. Ordered
# most-specific-first, same "ordered fallback list" shape as batch_runner.py's
# FIELD_SELECTORS. data-testid entries target modern SPA messaging drawers
# (X, LinkedIn) whose classnames are obfuscated but whose test hooks are not.
MESSAGE_FIELD_SELECTORS = [
    "div[data-testid='dmComposerTextInput']",  # X/Twitter DM composer
    "div[data-testid='tweetTextarea_0']",  # X composer fallback markup
    "div.msg-form__contenteditable[contenteditable='true']",  # LinkedIn message composer
    "div[data-testid*='message' i][contenteditable='true']",  # LinkedIn/other messaging drawers
    "div[aria-label='Message Body'][contenteditable='true']",  # Gmail compose body
    "div[role='textbox'][contenteditable='true']",  # generic rich-text composers
    "textarea[name='body']",
    "textarea[aria-label*='message' i]",
    "textarea[placeholder*='message' i]",
    "textarea",
]

# Detected and reported, NEVER clicked. This is the hard safety rail: no
# function in this module calls .click() on a selector from this list.
SEND_BUTTON_SELECTORS = [
    "button:has-text('Send')",
    "button[aria-label*='Send' i]",
    "button:has-text('Submit')",
    "input[type='submit']",
]

LINKEDIN_MESSAGE_BUTTON_RE = re.compile(r"^message$", re.IGNORECASE)

# Words that show up in a login/signup wall's URL, title, or page text. Kept
# broad on purpose - a false positive just means a tab is left for review
# instead of a fill attempt against login-page markup; a false negative means
# fill_message_box hangs or corrupts a login form, which is worse.
AUTH_WALL_TEXT_RE = re.compile(
    r"\b(log[\s-]?in|sign[\s-]?in|sign[\s-]?up|authwall|auth[\s_-]?wall|"
    r"unauthorized|create your account|join now to)\b",
    re.IGNORECASE,
)
# Login-form markup that shows up even when the URL/title don't say "login"
# (e.g. an in-place modal). Presence of a password field is the strongest
# single signal a real composer will not be on this page.
AUTH_WALL_SELECTORS = [
    "input[type='password']",
    "input[name='session_key']",  # LinkedIn login form
    "[data-testid='LoginForm']",  # X login form
    "[data-testid='ocfEnterTextTextInput']",  # X auth-wall interstitials
    "form[action*='login' i]",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError, ValueError):
        return default


# Role text drawn only from the profile URL itself (never invented) - e.g. a
# LinkedIn slug like ".../in/jane-cto-acme" or an X handle "@acme_founder".
# Matched against the URL with every non-alphanumeric run collapsed to a single
# space, so "_"/"-" separators (common in slugs and handles) count as word
# boundaries the same way a literal space would.
ROLE_HINT_PATTERNS = [
    (re.compile(r"\bcto\b", re.IGNORECASE), "CTO"),
    (re.compile(r"\bceo\b", re.IGNORECASE), "CEO"),
    (re.compile(r"\bco\s*founder\b", re.IGNORECASE), "Co-Founder"),
    (re.compile(r"\bfounder\b", re.IGNORECASE), "Founder"),
    (re.compile(r"\bvp\s*eng\w*\b", re.IGNORECASE), "VP of Engineering"),
]


def _infer_title_from_url(url: str) -> str:
    """Best-effort role guess from a profile URL's own path text. Returns '' if no hint is present."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", " ", url or "")
    for pattern, title in ROLE_HINT_PATTERNS:
        if pattern.search(normalized):
            return title
    return ""


def _infer_channel_from_url(url: str) -> str:
    """LinkedIn and X/Twitter profile links get their own channel; anything else is a contact form."""
    host = urlparse(url or "").netloc.lower()
    if "linkedin.com" in host:
        return "linkedin"
    if "x.com" in host or "twitter.com" in host:
        return "x"
    return "contact_form"


def normalize_target_record(raw: dict) -> dict:
    """
    Normalize the flat {company, website, contact_url, tech_stack, context} shape
    (no named contact, one profile link) into the {company, contacts: [...]}
    schema load_targets expects. Records that already carry a "contacts" list
    pass through untouched.
    """
    if "contacts" in raw:
        return raw

    contact_url = raw.get("contact_url", "")
    title = _infer_title_from_url(contact_url)
    normalized = dict(raw)
    normalized["contacts"] = [{
        "name": title or "Leadership Contact",
        "title": title,
        "channel": _infer_channel_from_url(contact_url),
        "profile_url": contact_url,
    }]
    tech_stack = raw.get("tech_stack")
    if isinstance(tech_stack, str) and tech_stack.strip():
        normalized["known_tech_stack"] = [
            item.strip() for item in tech_stack.split(",") if item.strip()
        ]
    return normalized


class RateLimiter:
    """Enforces a minimum delay between successive `wait()` calls."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            remaining = self.min_interval_seconds - (now - self._last_call)
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# Target loading
# ---------------------------------------------------------------------------
def load_targets(path: Path) -> list[dict]:
    """Load and validate the target list, dropping companies with no contacts."""
    raw = load_json(path, [])
    if not isinstance(raw, list):
        raise ValueError(f"Targets file must contain a JSON list: {path}")

    targets: list[dict] = []
    seen_companies: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        item = normalize_target_record(item)
        company = (item.get("company") or "").strip()
        if not company or company in seen_companies:
            continue
        contacts = [
            contact for contact in (item.get("contacts") or [])
            if isinstance(contact, dict) and contact.get("name")
        ]
        if not contacts:
            continue
        seen_companies.add(company)
        target = dict(item)
        target["company"] = company
        target["contacts"] = contacts
        targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# Tech-signal extraction
# ---------------------------------------------------------------------------
def extract_tech_signals(text: str) -> list[str]:
    """Return the unique tech-stack keywords found in text, in first-seen order."""
    seen: list[str] = []
    for match in TECH_SIGNAL_RE.finditer(text or ""):
        token = match.group(1)
        if token not in seen:
            seen.append(token)
    return seen


async def gather_tech_signals(page: Page, urls: list[str]) -> list[str]:
    """Best-effort scrape of public pages for tech-stack keywords; per-URL errors are swallowed."""
    combined_text = ""
    for url in urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            combined_text += " " + await page.locator("body").inner_text(timeout=5000)
        except Exception:
            continue
    return extract_tech_signals(combined_text)


# ---------------------------------------------------------------------------
# Gemini drafting
# ---------------------------------------------------------------------------
def draft_outreach_message(
    company: str,
    contact: dict,
    tech_signals: list[str],
    candidate_context_text: str,
    company_context: str = "",
) -> str | None:
    """Draft an ultra-concise, value-first cold message. None if drafting is unavailable."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  [draft] GEMINI_API_KEY is not set; message left blank for manual drafting.")
        return None

    prompt = f"""You write ultra-concise, value-first cold outreach from one engineer to another.

TRUSTED CANDIDATE CONTEXT (use only facts stated here; never invent employers, metrics, or credentials):
{candidate_context_text}

TARGET COMPANY: {company}
COMPANY CONTEXT: {company_context or "unknown"}
CONTACT: {contact.get("name", "")} ({contact.get("title", "")})
OBSERVED PUBLIC TECH SIGNALS: {", ".join(tech_signals) or "none observed"}

Write a short (under 90 words) first-person message to this contact. Requirements:
- Must NOT read like a cover letter or job application. No "I am excited to apply", no generic enthusiasm.
- Open with a specific, technical, value-first hook tied to one of the observed tech signals if any
  are present, and to one concrete detail from the trusted candidate context (e.g. a specific
  project, architecture decision, or measurable result described there).
- Sound like one engineer messaging another: direct, technical, no fluff, no flattery.
- End with a low-friction ask (a quick reply or a 15-minute call), not a request for a job.
- Plain text only: no markdown, no subject line, no signature block.

Return only the message text."""

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        message = (response.text or "").strip()
        message = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", message).strip()
        return message or None
    except Exception as error:
        print(f"  [draft] Gemini outreach drafting unavailable: {error}")
        return None


# ---------------------------------------------------------------------------
# SQLite tracking
# ---------------------------------------------------------------------------
def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                company TEXT NOT NULL,
                contact_name TEXT,
                contact_title TEXT,
                channel TEXT,
                url TEXT,
                message TEXT,
                status TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def log_state(
    db_path: Path, company: str, contact: dict, status: str, url: str = "", message: str = "",
) -> None:
    if status not in VALID_STATES:
        raise ValueError(f"Unknown outreach status: {status}")
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO outreach_events "
            "(timestamp, company, contact_name, contact_title, channel, url, message, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utc_now(), company, contact.get("name", ""), contact.get("title", ""),
                contact.get("channel", ""), url, message, status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_sent(db_path: Path, company: str, contact_name: str = "") -> bool:
    """
    Record that a human has confirmed sending a previously drafted message.

    This is the ONLY function that writes STATE_SENT, and it is only ever
    invoked from the `mark-sent` CLI command - never from the automated
    runner - keeping the "no automatic send" guarantee true for the whole log.
    """
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT contact_title, channel, url, message FROM outreach_events "
            "WHERE company = ? AND (? = '' OR contact_name = ?) "
            "ORDER BY id DESC LIMIT 1",
            (company, contact_name, contact_name),
        ).fetchone()
        if row is None:
            return False
        contact_title, channel, url, message = row
        conn.execute(
            "INSERT INTO outreach_events "
            "(timestamp, company, contact_name, contact_title, channel, url, message, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utc_now(), company, contact_name, contact_title, channel, url, message, STATE_SENT),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review-gated channel handling (never sends)
# ---------------------------------------------------------------------------
def build_outreach_url(contact: dict, message: str) -> tuple[str, bool]:
    """
    Return (url, body_prefilled) for the review tab.

    body_prefilled is True when the draft is already embedded in the URL
    (Gmail's compose deep link), so the caller should not also fill a field.
    """
    channel = (contact.get("channel") or "contact_form").lower()

    if channel == "email":
        email = contact.get("email", "")
        if not email:
            raise ValueError("email channel requires contact['email']")
        subject = contact.get("subject") or "Quick technical note"
        params = urlencode({"view": "cm", "fs": "1", "to": email, "su": subject, "body": message})
        return f"https://mail.google.com/mail/?{params}", True

    if channel == "linkedin":
        profile_url = contact.get("profile_url", "")
        if not profile_url:
            raise ValueError("linkedin channel requires contact['profile_url']")
        return profile_url, False

    if channel in ("x", "twitter"):
        profile_url = contact.get("profile_url", "")
        if not profile_url:
            raise ValueError("x channel requires contact['profile_url']")
        return profile_url, False

    contact_form_url = contact.get("contact_form_url") or contact.get("profile_url", "")
    if not contact_form_url:
        raise ValueError("contact_form channel requires contact['contact_form_url']")
    return contact_form_url, False


class NoComposerFoundError(RuntimeError):
    """No fillable message field was found - a private profile, or an unsupported layout."""


async def detect_auth_wall(page: Page) -> bool:
    """
    Best-effort check for a login/signup wall blocking the real composer.

    Checked right after navigation and before any fill attempt, so an
    unauthenticated session logs a clear auth_required state instead of
    fill_message_box silently failing against login-page markup.
    """
    try:
        if AUTH_WALL_TEXT_RE.search(page.url or ""):
            return True
    except Exception:
        pass
    try:
        if AUTH_WALL_TEXT_RE.search((await page.title()) or ""):
            return True
    except Exception:
        pass
    for selector in AUTH_WALL_SELECTORS:
        try:
            control = page.locator(selector).first
            if await control.count() and await control.is_visible():
                return True
        except Exception:
            continue
    return False


async def open_linkedin_message_composer(page: Page) -> None:
    """Open (not send) LinkedIn's message composer on an already-loaded profile page."""
    button = page.get_by_role("button", name=LINKEDIN_MESSAGE_BUTTON_RE).first
    try:
        await button.click(timeout=8000)
    except Exception as error:
        raise NoComposerFoundError(
            f"LinkedIn 'Message' button not found or not clickable: {error}"
        ) from error
    await page.wait_for_timeout(1000)


async def fill_message_box(page: Page, message: str) -> bool:
    """Fill the first visible message field with the draft. Never clicks send."""
    for selector in MESSAGE_FIELD_SELECTORS:
        try:
            locator = page.locator(selector).first
            if not await locator.count() or not await locator.is_visible():
                continue
        except Exception:
            continue
        tag = await locator.evaluate("e => e.tagName.toLowerCase()")
        if tag == "textarea":
            await locator.fill(message)
        else:
            await locator.click()
            await locator.type(message)
        print("  [ok] Filled outreach message draft")
        return True
    raise NoComposerFoundError("No visible message field found for this channel")


async def report_send_controls(page: Page) -> bool:
    """Detect (never click) a send/submit control so the review gate names it."""
    for selector in SEND_BUTTON_SELECTORS:
        try:
            control = page.locator(selector).first
            if await control.count() and await control.is_visible():
                print(f"  [review] Found a send control ({selector}) - NOT clicked. Send manually.")
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def prepare_outreach_target(
    context: BrowserContext,
    target: dict,
    semaphore: asyncio.Semaphore,
    db_path: Path,
    candidate_context_text: str,
    rate_limiter: RateLimiter,
) -> list[tuple[Page, str | None]]:
    """Research, draft, and open a review tab for every contact at one company."""
    company = target.get("company", "Unknown")
    signal_urls = [
        url for url in (target.get("blog_url"), target.get("careers_url"), target.get("website"))
        if url
    ]

    results: list[tuple[Page, str | None]] = []
    for contact in target["contacts"]:
        async with semaphore:
            await rate_limiter.wait()
            page = await context.new_page()
            label = f"{company} - {contact.get('name', 'contact')}"
            print(f"\n=== Researching: {label} ===")
            try:
                await asyncio.to_thread(log_state, db_path, company, contact, STATE_DISCOVERED)

                known_tech_stack = target.get("known_tech_stack")
                if known_tech_stack:
                    tech_signals = known_tech_stack
                else:
                    tech_signals = await gather_tech_signals(page, signal_urls)

                await rate_limiter.wait()
                message = await asyncio.to_thread(
                    draft_outreach_message,
                    company, contact, tech_signals, candidate_context_text,
                    target.get("context", ""),
                )
                if not message:
                    raise RuntimeError("No draft available; left for manual drafting")
                await asyncio.to_thread(
                    log_state, db_path, company, contact, STATE_DRAFTED, message=message
                )

                target_url, body_prefilled = build_outreach_url(contact, message)
                await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)

                if await detect_auth_wall(page):
                    await asyncio.to_thread(
                        log_state, db_path, company, contact, STATE_AUTH_REQUIRED,
                        url=target_url, message=message,
                    )
                    print(
                        f"  [review] Auth wall detected for {label}; log in manually in this "
                        "tab, then send yourself. Nothing was filled or sent."
                    )
                    results.append((page, label))
                    continue

                try:
                    if contact.get("channel", "").lower() == "linkedin":
                        await open_linkedin_message_composer(page)
                    if not body_prefilled:
                        await fill_message_box(page, message)
                except NoComposerFoundError as composer_error:
                    await asyncio.to_thread(
                        log_state, db_path, company, contact, STATE_NO_COMPOSER_FOUND,
                        url=target_url, message=message,
                    )
                    print(f"  [review] {label}: {composer_error} - tab left open for manual completion.")
                    results.append((page, label))
                    continue

                await report_send_controls(page)

                await asyncio.to_thread(
                    log_state, db_path, company, contact, STATE_REVIEWED,
                    url=target_url, message=message,
                )
                print("  [review] Draft ready - review and send manually. Nothing was sent.")
                results.append((page, None))
            except Exception as error:
                await asyncio.to_thread(
                    log_state, db_path, company, contact, STATE_ERROR, message=str(error)
                )
                print(f"  [error] {label}: {error}")
                results.append((page, label))
    return results


async def launch_browser_context(
    playwright, user_data_dir: Path | None,
) -> tuple[Browser | None, BrowserContext]:
    """
    Launch a persistent Chromium profile so LinkedIn/X/Gmail sessions already
    logged into that profile carry over, avoiding a fresh auth wall on every
    run. Falls back to a throwaway context (with a warning) if the profile
    can't be launched - most commonly because it is already locked by another
    running Chrome instance using the same --user-data-dir.

    Returns (browser, context). browser is None for a persistent context:
    Playwright ties its lifetime to the context itself, so callers must treat
    the context's own close as the signal everything is done.
    """
    if user_data_dir is not None:
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir), headless=False
            )
            print(f"  [ok] Reusing persistent browser profile: {user_data_dir}")
            return None, context
        except Exception as error:
            print(
                f"  [warn] Could not launch persistent profile at {user_data_dir} ({error}); "
                "falling back to a fresh, unauthenticated browser session."
            )
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    return browser, context


async def keep_review_tabs_open(
    context: BrowserContext, pages: list[Page], failed: list[str] | None = None,
) -> None:
    """
    Block until the human closes the review browser. Only listens on the
    context (not a separate Browser), since a persistent context has no
    separate Browser object to watch.
    """
    print(f"\nPrepared {len(pages)} tab(s). No submit buttons were clicked.")
    if failed:
        print(
            f"[warn] {len(failed)} of those did NOT finish and are partially "
            f"filled - do not submit without checking:"
        )
        for title in failed:
            print(f"    - {title}")
    print("Browser retention is active. Review and close the browser manually when finished.")
    disconnected = asyncio.Event()
    context.on("close", lambda _context: disconnected.set())
    await disconnected.wait()


async def run_outreach_campaign(
    targets_path: Path,
    db_path: Path,
    concurrency: int,
    min_interval: float,
    user_data_dir: Path | None = DEFAULT_USER_DATA_DIR,
) -> None:
    targets = load_targets(targets_path)
    if not targets:
        raise ValueError(f"No valid outreach targets found in {targets_path}")

    candidate_context_text = load_candidate_context()
    init_db(db_path)
    semaphore = asyncio.Semaphore(concurrency)
    rate_limiter = RateLimiter(min_interval)

    playwright = await async_playwright().start()
    _browser, context = await launch_browser_context(playwright, user_data_dir)

    nested_results = await asyncio.gather(
        *(
            prepare_outreach_target(
                context, target, semaphore, db_path, candidate_context_text, rate_limiter
            )
            for target in targets
        )
    )
    results = [pair for group in nested_results for pair in group]
    pages = [page for page, _ in results]
    failed = [label for _, label in results if label]
    open_pages = [page for page in pages if page is not None and not page.is_closed()]
    if open_pages:
        await open_pages[-1].bring_to_front()
    await keep_review_tabs_open(context, open_pages, failed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    if not 1 <= args.concurrency <= 20:
        raise SystemExit("--concurrency must be between 1 and 20")
    if args.min_interval < 0:
        raise SystemExit("--min-interval must be >= 0")
    user_data_dir = None if args.no_persistent_context else args.user_data_dir
    try:
        asyncio.run(
            run_outreach_campaign(
                args.targets, args.output, args.concurrency, args.min_interval, user_data_dir
            )
        )
    except KeyboardInterrupt:
        print("\nRunner stopped. Close any remaining browser windows manually.")
    except (FileNotFoundError, ValueError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    return 0


def cmd_mark_sent(args: argparse.Namespace) -> int:
    if mark_sent(args.output, args.company, args.contact):
        print(f"[ok] Marked '{args.company}' ({args.contact or 'any contact'}) as sent.")
        return 0
    print(
        f"[error] No prior outreach record found for '{args.company}' "
        f"({args.contact or 'any contact'}).",
        file=sys.stderr,
    )
    return 1


def cmd_sync_sheet(args: argparse.Namespace) -> int:
    from sheets_sync import sync_outreach_to_sheet

    try:
        sync_outreach_to_sheet(args.output)
    except Exception as exc:
        print(f"[error] sync-sheet failed: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Value-first outbound outreach agent. Never sends automatically."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Research targets, draft messages, and open review tabs"
    )
    run_parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH)
    run_parser.add_argument(
        "--output", type=Path, default=DEFAULT_DB_PATH,
        help="SQLite log for this campaign (use a distinct file per campaign)",
    )
    run_parser.add_argument("--concurrency", type=int, default=3)
    run_parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_SECONDS)
    run_parser.add_argument(
        "--user-data-dir", type=Path, default=DEFAULT_USER_DATA_DIR,
        help="Persistent Chromium profile dir, reused across runs to keep LinkedIn/X/Gmail "
             "logins (default: %(default)s)",
    )
    run_parser.add_argument(
        "--no-persistent-context", action="store_true",
        help="Always launch a fresh, unauthenticated browser session instead",
    )
    run_parser.set_defaults(func=cmd_run)

    mark_parser = subparsers.add_parser(
        "mark-sent", help="Record that a human has sent a previously drafted message"
    )
    mark_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    mark_parser.add_argument("--company", required=True)
    mark_parser.add_argument("--contact", default="")
    mark_parser.set_defaults(func=cmd_mark_sent)

    sync_parser = subparsers.add_parser(
        "sync-sheet", help="Push the outreach log to the tracker Google Sheet"
    )
    sync_parser.add_argument("--output", type=Path, default=DEFAULT_DB_PATH)
    sync_parser.set_defaults(func=cmd_sync_sheet)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
