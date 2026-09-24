"""
Small-business lead finder for the outreach agent.

Pulls businesses of one category in one area from OpenStreetMap (free, no API
key), checks each business's public website for gaps a small automation would
fix (no online booking, no contact form, no chat/text option, not mobile-ready,
no HTTPS, stale footer), and writes the ones with at least --min-issues gaps to
an outreach_agent.py targets file.

Every lead is written as target_type "small_business" with the site-check
findings in its context, so the drafted note opens with a problem that is
actually visible on that business's site instead of a generic pitch. Nothing
here contacts a business: it only reads public web pages, and drafts still go
through the outreach review gate.

Run:
    python lead_finder.py --category accountants --area "Tampa, FL"
    python lead_finder.py --category dentists --area "Austin, TX" --limit 40 \\
        --output queues/smb_dentists_austin.json
    python lead_finder.py --list-categories
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_PATH = BASE_DIR / "queues" / "smb_targets.json"
# Nominatim and Overpass both require an identifying User-Agent.
USER_AGENT = "job-agent-lead-finder/1.0 (small-business outreach research)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Public Overpass mirrors, tried in order: the free servers time out under load.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
REQUEST_TIMEOUT_SECONDS = 12
# The browser pass waits this long after load so script-injected booking and
# chat widgets have rendered before the page is re-checked.
BROWSER_SETTLE_MS = 3000
BROWSER_TIMEOUT_MS = 20000
HIGH_CONFIDENCE_SCORE = 65
MEDIUM_CONFIDENCE_SCORE = 40
AUDIT_WORKERS = 6
# Matches outreach_agent.PLACEHOLDER_CONTACT_NAME: no named person, so the
# drafter skips the greeting instead of inventing one.
PLACEHOLDER_CONTACT_NAME = "Leadership Contact"

# Category -> (human label, OSM tag filters). Home services are last on
# purpose: owners there are pitch-fatigued (2,000+ cold calls/texts got
# "heard it a million times"), so the defaults lean on professional offices.
CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "accountants": ("accounting and tax firm", ['office="accountant"', 'office="tax_advisor"']),
    "lawyers": ("law practice", ['office="lawyer"']),
    "dentists": ("dental practice", ['amenity="dentist"', 'healthcare="dentist"']),
    "physio": ("physical therapy clinic", ['healthcare="physiotherapist"']),
    "vets": ("veterinary clinic", ['amenity="veterinary"']),
    "property_managers": ("property management company", ['office="property_management"']),
    "insurance": ("insurance agency", ['office="insurance"']),
    "real_estate": ("real estate agency", ['office="estate_agent"']),
    "home_services": (
        "home services company",
        ['craft="hvac"', 'craft="plumber"', 'craft="electrician"', 'craft="roofer"'],
    ),
}

BOOKING_RE = re.compile(
    r"calendly\.com|acuityscheduling|setmore|squareup\.com/appointments|zocdoc|vagaro|"
    r"mindbody|janeapp|simplepractice|nexhealth|localmed|schedulicity|booksy|housecallpro|"
    r"servicetitan|jobber|book\s+(online|now)|appointment\s+request|"
    r"(book|schedule|request)\s+(your\s+|an?\s+)?(free\s+|personal\s+|new\s+patient\s+)?"
    r"(appointment|consultation|visit|exam|cleaning)",
    re.IGNORECASE,
)
CHAT_RE = re.compile(
    r"intercom|drift\.com|tawk\.to|livechat|podium|birdeye|tidio|crisp\.chat|olark|"
    r"zendesk|js\.hs-scripts|hubspot.*conversations|href=[\"']sms:|text\s+us",
    re.IGNORECASE,
)
FORM_RE = re.compile(r"<form\b(?![^>]*\brole=[\"']search)[\s\S]*?</form>", re.IGNORECASE)
FORM_FIELD_RE = re.compile(r"<textarea\b|type=[\"']email[\"']|name=[\"'][^\"']*(message|email)", re.I)
VIEWPORT_RE = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.IGNORECASE)
CONTACT_LINK_RE = re.compile(
    r"<a\b[^>]*href=[\"']([^\"'#]*(contact|quote|appointment|schedule)[^\"']*)[\"']", re.I
)
EMBED_FORM_RE = re.compile(r"jotform|typeform|wufoo|formstack|hsforms|forms\.gle|123formbuilder", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.IGNORECASE)
COPYRIGHT_YEAR_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
TITLE_RE = re.compile(r"<title[^>]*>([\s\S]*?)</title>", re.IGNORECASE)
# Addresses that are never a business inbox.
JUNK_EMAIL_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp)$|example\.|sentry|wixpress|godaddy|domain\.com|"
    r"yourname|email@|@2x|u003e",
    re.IGNORECASE,
)
# Listings that are not an owner-run small business, or where a cold note
# about their website would be inappropriate. Checked against name + title.
NOT_A_FIT_RE = re.compile(
    r"\b(lab|laboratory|labs|supply|supplies|supplier|equipment|wholesale|university|college|"
    r"find\s+local|school\s+of|(?<!animal\s)(?<!pet\s)hospital|health\s+system|uf\s+health|directory|family\s+office|forbes|"
    r"mental\s+health|behavioral|counseling|addiction|rehab|"
    # National chains: no owner to pitch, and corporate IT already owns the site.
    r"banfield|vca|bluepearl|aspen\s+dental|heartland\s+dental|western\s+dental|"
    r"coast\s+dental|morgan\s*(&|and)\s*morgan)\b",
    re.IGNORECASE,
)
# Words too generic to show a page belongs to a business ("Smith Dental LLC" ->
# {"smith", "dental"}). Trade words stay: a hijacked domain's page won't say "dental".
NAME_STOPWORDS = {"the", "and", "llc", "inc", "dds", "dmd", "cpa", "pllc", "ltd", "corp"}


@dataclass
class SiteAudit:
    """What the site check saw. `issues` are plain-English gaps, most useful first."""
    url: str
    reachable: bool = False
    issues: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    contact_page_url: str = ""
    title: str = ""


# ---------------------------------------------------------------------------
# OpenStreetMap lookup
# ---------------------------------------------------------------------------
def geocode_area(area: str, session: requests.Session) -> tuple[float, float, float, float]:
    """Return the (south, west, north, east) bounding box for a place name."""
    response = session.get(
        NOMINATIM_URL, params={"q": area, "format": "json", "limit": 1},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        raise ValueError(f"Could not find the area {area!r} on OpenStreetMap")
    south, north, west, east = (float(value) for value in results[0]["boundingbox"])
    return south, west, north, east


def build_overpass_query(tag_filters: list[str], bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL for every node/way carrying one of the tags and a website."""
    box = ",".join(f"{value:.5f}" for value in bbox)
    clauses = "".join(
        f'{kind}[{tag}]["{site_key}"]({box});'
        for tag in tag_filters
        for kind in ("node", "way")
        for site_key in ("website", "contact:website")
    )
    return f"[out:json][timeout:60];({clauses});out center tags;"


def parse_overpass_elements(elements: list[dict]) -> list[dict]:
    """Turn Overpass elements into business dicts, one per website domain."""
    businesses: list[dict] = []
    seen_domains: set[str] = set()
    for element in elements:
        tags = element.get("tags") or {}
        name = (tags.get("name") or "").strip()
        website = normalize_website(tags.get("website") or tags.get("contact:website") or "")
        if not name or not website:
            continue
        domain = site_domain(website)
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        city = tags.get("addr:city", "")
        state = tags.get("addr:state", "")
        businesses.append({
            "name": name,
            "website": website,
            "phone": tags.get("phone") or tags.get("contact:phone") or "",
            "email": tags.get("email") or tags.get("contact:email") or "",
            "city": ", ".join(part for part in (city, state) if part),
        })
    return businesses


def find_businesses(category: str, area: str, session: requests.Session) -> list[dict]:
    _label, tag_filters = CATEGORIES[category]
    bbox = geocode_area(area, session)
    query = build_overpass_query(tag_filters, bbox)
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            response = session.post(url, data={"data": query}, timeout=90)
            response.raise_for_status()
            return parse_overpass_elements(response.json().get("elements", []))
        except (requests.RequestException, ValueError) as error:
            last_error = error
            print(f"  [leads] {urlparse(url).hostname} failed ({error.__class__.__name__}); trying next")
    raise requests.RequestException(f"every Overpass server failed: {last_error}")


# ---------------------------------------------------------------------------
# Website check
# ---------------------------------------------------------------------------
def normalize_website(url: str) -> str:
    url = url.strip().split(";")[0].strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def site_domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def has_contact_form(html: str) -> bool:
    if EMBED_FORM_RE.search(html):
        return True
    return any(FORM_FIELD_RE.search(form) for form in FORM_RE.findall(html))


def extract_emails(html: str, domain: str) -> list[str]:
    """Business inboxes found on the page, same-domain addresses first."""
    found: list[str] = []
    for email in MAILTO_RE.findall(html) + EMAIL_RE.findall(unescape(html)):
        email = email.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(email) or JUNK_EMAIL_RE.search(email) or email in found:
            continue
        found.append(email)
    return sorted(found, key=lambda e: not e.endswith("@" + domain) and not e.endswith("." + domain))


def audit_html(
    url: str, final_url: str, html: str, contact_html: str = "", current_year: int | None = None
) -> SiteAudit:
    """Pure check of fetched pages: what is missing that a small automation would fix."""
    current_year = current_year or datetime.now().year
    combined = html + "\n" + contact_html
    audit = SiteAudit(url=final_url or url, reachable=True)
    title_match = TITLE_RE.search(html)
    audit.title = " ".join(unescape(title_match.group(1)).split()) if title_match else ""

    if not BOOKING_RE.search(combined):
        audit.issues.append("no way to book or request an appointment online; visitors have to call")
    if not has_contact_form(combined):
        audit.issues.append("no contact or quote form, so after-hours visitors have no way to reach them")
    if not CHAT_RE.search(combined):
        audit.issues.append("no chat or text-us option on the site")
    if not VIEWPORT_RE.search(html):
        audit.issues.append("the site is not set up for phones (no mobile viewport)")
    if final_url.lower().startswith("http://"):
        audit.issues.append("the site does not load over a secure connection (no HTTPS)")
    years = [int(year) for year in COPYRIGHT_YEAR_RE.findall(html) if 1995 <= int(year) <= current_year]
    if years and max(years) <= current_year - 3:
        audit.issues.append(f"the footer still says {max(years)}, so the site looks unmaintained")

    audit.emails = extract_emails(combined, site_domain(final_url or url))
    return audit


def fetch_and_audit(url: str, session: requests.Session) -> SiteAudit:
    """Fetch the homepage (and its contact page, if linked) and audit them."""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        if response.status_code >= 400 and url.startswith("https://"):
            response = session.get("http://" + url.removeprefix("https://"),
                                   timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
        return SiteAudit(url=url)
    html = response.text[:500_000]
    contact_html, contact_url = "", ""
    link = CONTACT_LINK_RE.search(html)
    if link:
        contact_url = urljoin(response.url, unescape(link.group(1)))
        if site_domain(contact_url) == site_domain(response.url):
            try:
                contact_response = session.get(contact_url, timeout=REQUEST_TIMEOUT_SECONDS)
                if contact_response.ok:
                    contact_html = contact_response.text[:500_000]
            except requests.RequestException:
                contact_url = ""
        else:
            contact_url = ""
    audit = audit_html(url, response.url, html, contact_html)
    audit.contact_page_url = contact_url
    return audit


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", unescape(name).lower())
    return {w for w in words if len(w) > 2 and w not in NAME_STOPWORDS}


def fit_problem(business: dict, audit: SiteAudit, listed_url: str) -> str:
    """Why this reachable site should not become a lead, or '' if it should."""
    name = business.get("name", "")
    if NOT_A_FIT_RE.search(f"{name} {audit.title}"):
        return "not an owner-run small business"
    tokens = name_tokens(name)
    final_domain = site_domain(audit.url)
    # A redirect to a domain carrying none of the business's name means it moved or merged;
    # one that keeps the name (smilecreatorsmiami.com -> smilecreators.com) is just a rename.
    if final_domain != site_domain(listed_url) and not any(t in final_domain for t in tokens):
        return f"redirects to another domain ({final_domain}); moved, merged, or rebranded"
    haystack = f"{audit.title} {final_domain}".lower()
    if tokens and not any(token in haystack for token in tokens):
        return "page title does not mention the business; the domain may have changed hands"
    return ""


def site_context(description: str, issues: list[str]) -> str:
    """The drafting context: who they are, then only the gaps still believed true."""
    return f"{description} Site check of their public website found: " + "; ".join(issues) + "."


# ---------------------------------------------------------------------------
# Browser check: the static fetch never runs scripts, so a booking or chat
# widget injected by JavaScript reads as "missing". Re-check each lead in a
# real headless browser and keep only the gaps that survive.
# ---------------------------------------------------------------------------
def reconcile_findings(static_issues: list[str], rendered_issues: list[str]) -> tuple[list[str], list[str]]:
    """(verified, disproven): static gaps the rendered page confirms, and ones it contradicts."""
    rendered = set(rendered_issues)
    verified = [issue for issue in static_issues if issue in rendered]
    disproven = [issue for issue in static_issues if issue not in rendered]
    return verified, disproven


def _rendered_html(page, url: str) -> tuple[str, str]:
    page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
    page.wait_for_timeout(BROWSER_SETTLE_MS)
    return page.content(), page.url


def browser_check_targets(targets: list[dict], force: bool = False) -> int:
    """Re-audit each target's rendered pages in headless Chromium; update findings in place."""
    from playwright.sync_api import sync_playwright

    pending = [t for t in targets if force or not t.get("browser_checked")]
    if not pending:
        return 0
    print(f"[leads] Browser-checking {len(pending)} site(s) with scripts running...")
    checked = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        for target in pending:
            page = context.new_page()
            try:
                html, final_url = _rendered_html(page, target["website"])
                contact_html = ""
                contact_url = target.get("contact_page_url", "")
                link = CONTACT_LINK_RE.search(html)
                if not contact_url and link:
                    contact_url = urljoin(final_url, unescape(link.group(1)))
                if contact_url and site_domain(contact_url) == site_domain(final_url):
                    contact_html, _ = _rendered_html(page, contact_url)
            except Exception as error:
                print(f"  [browser] {target['company']}: could not render ({error.__class__.__name__})")
                continue
            finally:
                page.close()
            rendered = audit_html(target["website"], final_url, html, contact_html)
            static = target.get("static_findings") or target.get("audit_findings", [])
            verified, disproven = reconcile_findings(static, rendered.issues)
            description = target.get("context", "").split(" Site check")[0]
            target.update({
                "static_findings": static,
                "audit_findings": verified,
                "disproven_findings": disproven,
                "browser_checked": True,
                "site_title": rendered.title,
                "context": site_context(description, verified) if verified else description,
            })
            checked += 1
            if disproven:
                print(f"  [browser] {target['company']}: not actually missing - {'; '.join(disproven)}")
        browser.close()
    return checked


# ---------------------------------------------------------------------------
# Confidence: how sure we are the note is both true and deliverable.
# ---------------------------------------------------------------------------
def score_target(target: dict) -> dict:
    """{"score": 0-100, "label": high|medium|low|skip, "reasons": [...]} for review ordering."""
    if NOT_A_FIT_RE.search(f"{target.get('company', '')} {target.get('site_title', '')}"):
        return {"score": 0, "label": "skip", "reasons": ["national chain or not owner-run"]}
    issues = target.get("audit_findings", [])
    if not issues:
        return {"score": 0, "label": "skip", "reasons": ["no gap left after the browser check"]}
    reasons, score = [], 0
    # A note pitches one gap, so past two more gaps add little; where the email
    # lands matters more (an off-domain address is often the web vendor's).
    if target.get("browser_checked"):
        score += min(len(issues), 2) * 20 + (5 if len(issues) > 2 else 0)
        reasons.append(f"{len(issues)} gap(s) confirmed in a real browser")
    else:
        score += min(len(issues), 2) * 8
        reasons.append(f"{len(issues)} gap(s), not browser-checked")
    disproven = target.get("disproven_findings", [])
    if disproven:
        score -= 15 * len(disproven)
        reasons.append(f"{len(disproven)} gap(s) the static check got wrong")
    contact = (target.get("contacts") or [{}])[0]
    if contact.get("channel") == "email":
        email_domain = contact.get("email", "").rsplit("@", 1)[-1].lower()
        domain = site_domain(target.get("website", ""))
        if email_domain == domain or email_domain.endswith("." + domain):
            score += 25
            reasons.append("email on their own domain")
        else:
            score += 12
            reasons.append(f"email on {email_domain}, not their domain")
    else:
        score += 5
        reasons.append("contact form only")
    score = max(0, min(100, score))
    label = ("high" if score >= HIGH_CONFIDENCE_SCORE
             else "medium" if score >= MEDIUM_CONFIDENCE_SCORE else "low")
    return {"score": score, "label": label, "reasons": reasons}


def score_and_sort(targets: list[dict]) -> list[dict]:
    for target in targets:
        target["confidence"] = score_target(target)
    return sorted(targets, key=lambda t: -t["confidence"]["score"])


def print_confidence_table(targets: list[dict]) -> None:
    for target in targets:
        confidence = target["confidence"]
        print(f"  {confidence['score']:>3} {confidence['label']:<6} {target['company'][:40]:<40} "
              f"{'; '.join(confidence['reasons'])}")


# ---------------------------------------------------------------------------
# Targets output
# ---------------------------------------------------------------------------
def build_target(business: dict, audit: SiteAudit, category: str) -> dict | None:
    """An outreach_agent.py target for this business, or None with no way to reach them."""
    label = CATEGORIES[category][0]
    email = business.get("email") or (audit.emails[0] if audit.emails else "")
    if email:
        contact = {"name": PLACEHOLDER_CONTACT_NAME, "title": "", "channel": "email", "email": email}
    elif audit.contact_page_url:
        contact = {"name": PLACEHOLDER_CONTACT_NAME, "title": "", "channel": "contact_form",
                   "contact_form_url": audit.contact_page_url}
    else:
        return None
    contact["subject"] = f"Quick fix for {business['name']}'s website"
    where = f" in {business['city']}" if business.get("city") else ""
    return {
        "company": business["name"],
        "website": audit.url,
        "target_type": "small_business",
        "hook": "reverse_audit",
        "context": site_context(f"Small {label}{where}.", audit.issues),
        "audit_findings": audit.issues,
        "contact_page_url": audit.contact_page_url,
        "phone": business.get("phone", ""),
        "category": category,
        "source": "openstreetmap",
        "contacts": [contact],
    }


def merge_targets(existing: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    """Append new targets whose website domain and company are not already queued."""
    domains = {site_domain(t.get("website", "")) for t in existing}
    companies = {(t.get("company") or "").strip().lower() for t in existing}
    merged, added = list(existing), 0
    for target in new:
        domain, company = site_domain(target["website"]), target["company"].strip().lower()
        if domain in domains or company in companies:
            continue
        merged.append(target)
        domains.add(domain)
        companies.add(company)
        added += 1
    return merged, added


def run(
    category: str, area: str, limit: int, min_issues: int, output: Path, browser_check: bool = True
) -> int:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    print(f"[leads] Looking up {CATEGORIES[category][0]}s in {area} on OpenStreetMap...")
    businesses = find_businesses(category, area, session)
    print(f"[leads] {len(businesses)} with a website; checking up to {limit}.")
    businesses = businesses[:limit]

    site_session = requests.Session()
    site_session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
    with ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as pool:
        audits = list(pool.map(lambda b: fetch_and_audit(b["website"], site_session), businesses))

    targets, unreachable, not_a_fit, too_few, no_channel = [], 0, 0, 0, 0
    for business, audit in zip(businesses, audits):
        if not audit.reachable:
            unreachable += 1
            continue
        reason = fit_problem(business, audit, business["website"])
        if reason:
            not_a_fit += 1
            print(f"  [skip] {business['name']}: {reason}")
            continue
        if len(audit.issues) < min_issues:
            too_few += 1
            continue
        target = build_target(business, audit, category)
        if target is None:
            no_channel += 1
            continue
        targets.append(target)
        print(f"  [lead] {business['name']}: {len(audit.issues)} gap(s) - {audit.issues[0]}")

    if browser_check and targets:
        browser_check_targets(targets)
        kept = [t for t in targets if len(t["audit_findings"]) >= min_issues]
        for target in targets:
            if target not in kept:
                print(f"  [drop] {target['company']}: under {min_issues} gap(s) once scripts ran")
        too_few += len(targets) - len(kept)
        targets = kept

    existing = load_targets_file(output)
    merged, added = merge_targets(existing, targets)
    write_targets_file(output, score_and_sort(merged))
    print(
        f"[leads] Added {added} new lead(s) to {output} "
        f"(skipped: {unreachable} unreachable, {not_a_fit} not a fit, "
        f"{too_few} under {min_issues} gap(s), "
        f"{no_channel} with no email or contact page)."
    )
    return added


def load_targets_file(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8") or "[]") if path.exists() else []


def write_targets_file(path: Path, targets: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(targets, indent=2) + "\n", encoding="utf-8")


def rescore(output: Path, browser_check: bool = True, force: bool = False) -> list[dict]:
    """Browser-check (once) and re-rank every lead already in a targets file."""
    targets = load_targets_file(output)
    if browser_check:
        browser_check_targets(targets, force=force)
    targets = score_and_sort(targets)
    write_targets_file(output, targets)
    print(f"[leads] Ranked {len(targets)} lead(s) in {output}, most confident first:")
    print_confidence_table(targets)
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find small businesses with fixable website gaps and queue them for outreach."
    )
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--category", choices=sorted(CATEGORIES), help="Kind of business")
    parser.add_argument("--area", help='City or region, e.g. "Tampa, FL"')
    parser.add_argument("--limit", type=int, default=30, help="Max sites to check (default: %(default)s)")
    parser.add_argument("--min-issues", type=int, default=2,
                        help="Gaps a site needs before it becomes a lead (default: %(default)s)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--list-categories", action="store_true")
    parser.add_argument("--rescore", action="store_true",
                        help="Browser-check and re-rank the leads already in --output")
    parser.add_argument("--recheck", action="store_true",
                        help="With --rescore, browser-check leads that were already checked")
    parser.add_argument("--no-browser-check", action="store_true",
                        help="Skip the headless-browser pass (faster, less accurate)")


def cmd_find_leads(args: argparse.Namespace) -> int:
    if args.list_categories:
        for key, (label, _tags) in CATEGORIES.items():
            print(f"{key:18} {label}")
        return 0
    if args.rescore:
        rescore(args.output, browser_check=not args.no_browser_check, force=args.recheck)
        return 0
    if not args.category or not args.area:
        print("[error] --category and --area are required", file=sys.stderr)
        return 2
    try:
        run(args.category, args.area, args.limit, args.min_issues, args.output,
            browser_check=not args.no_browser_check)
    except (requests.RequestException, ValueError) as exc:
        print(f"[error] lead search failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return cmd_find_leads(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
