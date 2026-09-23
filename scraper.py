"""
Job board scraper for the job-agent repo.

Queries Greenhouse, Ashby, Lever and Workable board APIs, filters by keyword and
location, and writes matching job URLs to queues/pending_jobs.json so
cli.py's `batch-run` command can iterate through them.

Run:
    python scraper.py greenhouse <board_token> [keyword1 keyword2 ...]
    python scraper.py ashby <org_name> [keyword1 keyword2 ...]
    python scraper.py all [keyword1 keyword2 ...]

Separate searches write to separate queues, so one person's results never land
in the queue another person is about to apply from:

    python scraper.py all --roles "Graphic Designer, Visual Designer" \
        --output queues/design_jobs.json --allow-senior

--allow-senior matters there: the default seniority filter is tuned for a junior
engineering search and would otherwise drop "Senior Graphic Designer". See
--help for --exclude-titles, which narrows the filter instead of removing it.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BASE_DIR / "queues"
PENDING_JOBS_PATH = QUEUE_DIR / "pending_jobs.json"

# Tuned by dry sweep on 2026-09-20 across 90 boards. Bare "Software Engineer"
# and "Developer" are deliberately absent: they are title prefixes at nearly
# every company, so adding them took a sweep from 92 to 323 matches at 19%
# precision - 138 came from "Software Engineer" alone, mostly billing, web
# products, developer advocacy and 2027 internships. The specific multi-word
# terms below reach the same infra/inference/data-platform roles at 86%
# precision instead.
DEFAULT_KEYWORDS = [
    "Data Scientist",
    "Data Analyst",
    "AI Engineer",
    "Machine Learning",
    "Backend",
    "Python",
    "Automation",
    "Data Engineer",
    "ML Engineer",
    "MLOps",
    "Analytics Engineer",
    "Platform Engineer",
    "Infrastructure Engineer",
    "LLM",
    "Inference",
    "Data Platform",
    "Applied Scientist",
]

# Word-boundary pattern so "us" doesn't false-match inside words like "Austin"/"Russia".
US_TOKEN_RE = re.compile(r"\b(us|usa|u\.s\.|united states)\b", re.IGNORECASE)
# US territories are the United States and need no sponsorship, but their
# postings rarely carry a "US" token - "San Juan, PR" names neither the country
# nor "remote", so the filter below dropped every on-island role. Matched only
# against a location field, where a bare "PR" means Puerto Rico rather than
# public relations.
US_TERRITORY_RE = re.compile(
    r"\b(puerto\s+rico|pr|"
    r"u\.?s\.?\s*virgin\s+islands|usvi|vi|"
    r"guam|gu|american\s+samoa|northern\s+mariana|saipan|"
    # Larger PR municipalities, which postings often name on their own.
    r"san\s+juan|bayam[oó]n|carolina|ponce|caguas|guaynabo|mayag[uü]ez|"
    r"arecibo|dorado|humacao|aguadilla|trujillo\s+alto|toa\s+baja|"
    r"cataño|rio\s+piedras|r[ií]o\s+piedras|hato\s+rey)\b",
    re.IGNORECASE,
)
REMOTE_TOKEN_RE = re.compile(r"\bremote\b", re.IGNORECASE)
# If any of these appear alongside "remote", the listing is for a non-US region
# even though it contains the word "remote" (e.g. "Poland - Remote", "Ontario - Remote").
NON_US_REGION_RE = re.compile(
    # Countries and regions
    r"\b(canada|ontario|india|poland|mexico|brazil|germany|france|spain|"
    r"italy|netherlands|ireland|australia|singapore|japan|china|emea|apac|latam|"
    r"uk|united kingdom|israel|argentina|colombia|chile|portugal|sweden|"
    r"norway|denmark|finland|switzerland|austria|belgium|romania|ukraine|"
    r"czechia|czech republic|hungary|greece|turkey|egypt|nigeria|kenya|"
    r"south africa|korea|taiwan|thailand|vietnam|philippines|indonesia|"
    r"malaysia|new zealand|"
    # Latin America and the Caribbean. These were missing, so "Remote - Peru"
    # and "Remote (Guatemala)" passed the bare-"remote" rule as if US-based.
    # Costa Rica and the Dominican Republic also matter for a second reason:
    # they share city names (San Juan, Carolina) with Puerto Rico.
    r"costa rica|dominican republic|el salvador|guatemala|honduras|"
    r"nicaragua|panama|paraguay|uruguay|bolivia|ecuador|venezuela|peru|"
    r"cuba|haiti|jamaica|trinidad|tobago|barbados|bahamas|belize|guyana|"
    r"suriname|"
    # Non-US tech hub cities (postings often name only the city)
    r"toronto|vancouver|montreal|ottawa|calgary|waterloo|"
    r"bangalore|bengaluru|hyderabad|pune|gurugram|gurgaon|noida|mumbai|"
    r"delhi|chennai|kolkata|ahmedabad|"
    r"london|manchester|edinburgh|dublin|berlin|munich|hamburg|paris|"
    r"amsterdam|barcelona|madrid|lisbon|milan|rome|zurich|geneva|vienna|"
    r"stockholm|copenhagen|oslo|helsinki|warsaw|krakow|prague|budapest|"
    r"bucharest|kyiv|kiev|tel aviv|dubai|"
    r"sydney|melbourne|brisbane|auckland|wellington|"
    r"tokyo|osaka|seoul|beijing|shanghai|shenzhen|hong kong|taipei|"
    r"bangkok|manila|jakarta|kuala lumpur|ho chi minh|hanoi|"
    r"sao paulo|rio de janeiro|buenos aires|bogota|santiago|lima|"
    r"mexico city|guadalajara|monterrey)\b",
    re.IGNORECASE,
)

# Many boards give a bare "City, ST" with no country token - Lever's own demo
# board returns "Baltimore, MD" - which the rules above would drop exactly as
# they dropped "San Juan, PR". Full state names match anywhere; the two-letter
# codes are matched CASE-SENSITIVELY and only after a comma, because lowercase
# "or", "in", "me", "hi", "ok", "de", "la", "pa" and "co" are ordinary words and
# "DE"/"IN"/"CA" also read as Germany/India/Canada. The non-US guard still
# applies, so "Toronto, CA" and "Berlin, DE" stay out.
US_STATE_NAME_RE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"district of columbia)\b",
    re.IGNORECASE,
)
US_STATE_ABBR_RE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VA|VT|WA|WI|WV|WY)\b"
)

REQUEST_HEADERS = {"User-Agent": "job-agent-scraper/1.0 (+https://github.com/)"}

GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
ASHBY_JOBS_URL = "https://api.ashbyhq.com/posting-api/job-board/{org_name}"
LEVER_JOBS_URL = "https://api.lever.co/v0/postings/{org_name}?mode=json"
# v3 is a POST search endpoint; the GET form 404s. Pages 10 at a time.
# v1 widget returns every posting in one response and is rate-limited
# separately from v3, so it is tried first. v3 is a POST search (GET 404s).
WORKABLE_V1_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
WORKABLE_V3_URL = "https://apply.workable.com/api/v3/accounts/{account}/jobs"
WORKABLE_MAX_PAGES = 40

REQUEST_TIMEOUT = 15
# Workable rate-limits: a burst of board requests returns 429, and because
# scrape_target_companies logs-and-skips any exception, a 429 silently drops a
# board from the run. That is indistinguishable from "this company has no
# openings", so retry instead of skipping.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 3.0
# Workable rate-limits per account burst; a small gap between pages avoids
# tripping it in the first place, which retrying afterwards cannot undo.
WORKABLE_PAGE_DELAY_SECONDS = 0.75


class RateLimitedError(RuntimeError):
    """A board was not scraped because the API kept refusing, not because it was empty."""


def _request_with_retry(method: str, url: str, **kwargs):
    """
    Issue a request, retrying transient statuses with backoff.

    Honours Retry-After when the server sends it. Returns the final response,
    including a still-failing one, so each caller keeps its own 404 handling.
    """
    response = None
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            # A timeout or reset is transient and must not cost a whole board:
            # scrape_target_companies logs-and-skips, so one dropped connection
            # reads as "no openings" exactly like a 429 did. Seen live - the
            # anthropic board was lost from a sweep this way, taking 10 real
            # matches with it.
            last_error, response = exc, None
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(min(RETRY_BACKOFF_SECONDS * (2 ** attempt), 30.0))
            continue
        if response.status_code not in RETRY_STATUSES:
            return response
        if attempt == MAX_RETRIES - 1:
            break
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else RETRY_BACKOFF_SECONDS * (2 ** attempt)
        except ValueError:
            delay = RETRY_BACKOFF_SECONDS * (2 ** attempt)
        time.sleep(min(delay, 30.0))
    if response is None and last_error is not None:
        raise RateLimitedError(
            f"{url} unreachable after {MAX_RETRIES} attempts ({type(last_error).__name__})"
        )
    if response is not None and response.status_code in RETRY_STATUSES:
        raise RateLimitedError(
            f"{url} still returned {response.status_code} after {MAX_RETRIES} attempts"
        )
    return response

# Default target company list for scrape_target_companies(). Edit freely to add/remove boards.
# Every slug below was verified against the live board API and returned real
# postings. A company's platform is not guessable from its name - anthropic,
# scaleai and vercel all 404'd or returned empty on Ashby and only work on
# Greenhouse - so probe before adding rather than assuming.
TARGET_COMPANIES = {
    "ashby": [
        # Mid-market / remote-first, probed 2026-09-20.
        "1password", "buffer", "clerk", "close", "gitbook", "hopper", "hubstaff",
        "loom", "miro", "mural", "neon", "posthog", "railway", "resend", "zapier",
        "angi", "baseten", "betterup", "clickhouse", "cohere", "confluent",
        "cursor", "elevenlabs", "hex", "langchain", "linear", "modal",
        "notion", "openai", "pinecone", "plaid", "ramp", "render", "replit",
        "sentry", "snowflake", "supabase", "vanta", "weaviate",
    ],
    # Probed 2026-09-20. Lever: 63 candidates tried, only these answered; the
    # ones at 0 postings are real boards that happened to be empty, so they are
    # kept rather than dropped.
    "lever": [
        "brightwheel", "clari", "metabase", "olo", "outreach", "tecton", "whoop",
        "fly",
    ],
    # Workable's verified accounts skew European, so most yield nothing under the
    # US filter. Only these two have US postings; blueground, epignosis, skroutz,
    # spotawheel and upstream are valid slugs but were EU-only when probed, and
    # are left out rather than spend a request each run for nothing.
    "workable": [
        "persado", "orfium",
        # Puerto Rico. Confirmed real accounts by the proper-cased "name" the
        # widget endpoint returns ("Wovenware", "BrainHi", "TrueNorth"); all had
        # zero openings when probed, so they contribute nothing until they post.
        # Kept because they are the only PR boards found on any of the four
        # platforms. "popular" echoed back lowercase - the generic-response
        # pattern, not Banco Popular - so it is deliberately left out.
        "wovenware", "brainhi", "truenorth",
    ],
    "greenhouse": [
        # Mid-market / remote-first, probed 2026-09-20.
        "aha", "bitwarden", "planetscale", "tailscale",
        "affirm", "airtable", "anthropic", "asana", "brex", "chime",
        "cloudflare", "coinbase", "databricks", "datadog", "discord",
        "duolingo", "elastic", "figma", "fivetran", "flexport", "gitlab",
        "gusto", "instacart", "lyft", "mongodb", "netlify", "pinterest",
        "reddit", "remotecom", "robinhood", "samsara", "scaleai",
        "squarespace", "stripe", "twilio", "vercel",
    ],
}

# Titles carrying these tokens are senior/leadership postings and are dropped
# regardless of keyword match. Word-boundary matching so "lead" doesn't hit
# "Leadership Development" false-positives inside longer words.
# "intern" is grouped here because this is the junior-level gate and a student
# posting is as unreachable as a director one. Written as intern(ship)?s? so it
# catches "Intern" and "Internship" while leaving "Internal Tools" and
# "International" alone - a bare \bintern\b would miss "Internship" entirely.
SENIORITY_EXCLUDE_RE = re.compile(
    r"\b(senior|sr|staff|principal|lead|director|vp|vice president|"
    r"head of|manager|executive|chief|distinguished|fellow|architect|"
    r"intern(?:ship)?s?)\b",
    re.IGNORECASE,
)


def is_excluded_seniority(title: str, exclude_re=SENIORITY_EXCLUDE_RE) -> bool:
    """True if a title reads as senior/leadership and should be skipped."""
    if exclude_re is None:
        return False
    return bool(exclude_re.search(title or ""))


def _title_matches_keywords(
    title: str, keywords: list[str], exclude_re=SENIORITY_EXCLUDE_RE
) -> bool:
    """
    Keyword match, gated so senior/leadership titles never qualify.

    exclude_re is a parameter rather than the global because the default is
    tuned for a junior engineering search: it drops "Senior Graphic Designer"
    and "Lead Visual Designer", which is wrong for a design search run by
    someone else. Pass None to keep every seniority, or a narrower pattern to
    drop only the levels you actually want gone.
    """
    if is_excluded_seniority(title, exclude_re):
        return False
    title_lower = title.lower()
    return any(keyword.lower() in title_lower for keyword in keywords)


def matches_location(location_text: str) -> bool:
    """
    Filter helper: True if a job's location text suggests Remote/US alignment.
    Requires an explicit US signal, or a bare "remote" with no other region
    named (e.g. "US - Remote" passes, "Poland - Remote" does not).
    """
    if not location_text:
        return False
    if US_TOKEN_RE.search(location_text):
        return True
    if US_TERRITORY_RE.search(location_text) and not NON_US_REGION_RE.search(location_text):
        return True
    if (US_STATE_NAME_RE.search(location_text) or US_STATE_ABBR_RE.search(location_text)) \
            and not NON_US_REGION_RE.search(location_text):
        return True
    if REMOTE_TOKEN_RE.search(location_text) and not NON_US_REGION_RE.search(location_text):
        return True
    return False


def fetch_greenhouse_jobs(
    board_token: str, keywords: list[str] | None = None,
    exclude_re=SENIORITY_EXCLUDE_RE,
) -> list[dict]:
    """Query a Greenhouse board API and return jobs matching keywords + location."""
    keywords = keywords or DEFAULT_KEYWORDS
    url = GREENHOUSE_JOBS_URL.format(board_token=board_token)

    response = _request_with_retry("GET", url, headers=REQUEST_HEADERS)
    if response.status_code == 404:
        raise ValueError(f"Greenhouse board '{board_token}' not found (404). Check the board token.")
    response.raise_for_status()
    jobs = response.json().get("jobs", [])

    matches = []
    for job in jobs:
        title = job.get("title", "")
        if not _title_matches_keywords(title, keywords, exclude_re):
            continue

        location_text = (job.get("location") or {}).get("name", "")
        if not matches_location(location_text):
            continue

        matches.append({
            "title": title,
            "url": job.get("absolute_url", ""),
            "company": board_token,
            "location": location_text,
            "source": "greenhouse",
        })

    return matches


def fetch_ashby_jobs(
    org_name: str, keywords: list[str] | None = None,
    exclude_re=SENIORITY_EXCLUDE_RE,
) -> list[dict]:
    """Pull listings from an Ashby job board API and return jobs matching keywords + location."""
    keywords = keywords or DEFAULT_KEYWORDS
    url = ASHBY_JOBS_URL.format(org_name=org_name)

    response = _request_with_retry("GET", url, headers=REQUEST_HEADERS)
    if response.status_code == 404:
        raise ValueError(f"Ashby org '{org_name}' not found (404). Check the org name in the job board URL.")
    response.raise_for_status()
    jobs = response.json().get("jobs", [])

    matches = []
    for job in jobs:
        title = job.get("title", "")
        if not _title_matches_keywords(title, keywords, exclude_re):
            continue

        location_text = job.get("location", "") or job.get("locationName", "")
        if not matches_location(location_text):
            continue

        matches.append({
            "title": title,
            "url": job.get("jobUrl") or job.get("applyUrl", ""),
            "company": org_name,
            "location": location_text,
            "source": "ashby",
        })

    return matches


def fetch_lever_jobs(
    org_name: str, keywords: list[str] | None = None,
    exclude_re=SENIORITY_EXCLUDE_RE,
) -> list[dict]:
    """
    Pull listings from a Lever board API and return jobs matching keywords + location.

    Verified against the live API: the response is a bare JSON list, the title
    lives in "text" (not "title"), and the location is under
    categories.location with categories.allLocations holding every location for
    a multi-site posting. workplaceType == "remote" is folded into the location
    text so a remote posting that names only a city still reads as remote; the
    non-US guard in matches_location keeps a remote-but-foreign role out.
    """
    keywords = keywords or DEFAULT_KEYWORDS
    url = LEVER_JOBS_URL.format(org_name=org_name)

    response = _request_with_retry("GET", url, headers=REQUEST_HEADERS)
    if response.status_code == 404:
        raise ValueError(f"Lever org '{org_name}' not found (404). Check the org name in the board URL.")
    response.raise_for_status()
    postings = response.json()
    if not isinstance(postings, list):
        raise ValueError(f"Lever org '{org_name}' returned an unexpected payload (not a list).")

    matches = []
    for job in postings:
        title = job.get("text", "")
        if not _title_matches_keywords(title, keywords, exclude_re):
            continue

        categories = job.get("categories") or {}
        all_locations = categories.get("allLocations") or []
        location_text = " • ".join(all_locations) if all_locations else (categories.get("location") or "")
        if str(job.get("workplaceType", "")).lower() == "remote" and "remote" not in location_text.lower():
            location_text = f"{location_text} • Remote".strip(" •")
        if not matches_location(location_text):
            continue

        matches.append({
            "title": title,
            "url": job.get("hostedUrl") or job.get("applyUrl", ""),
            "company": org_name,
            "location": location_text,
            "source": "lever",
        })

    return matches


def _workable_location_text(city, region, country, locations, remote) -> str:
    """Build one location string from Workable's split city/region/country fields."""
    text = ", ".join(part for part in (city, region, country) if part)
    if not text and locations:
        first = locations[0] or {}
        text = ", ".join(
            part for part in (first.get("city"), first.get("region"), first.get("country")) if part
        )
    if remote and "remote" not in text.lower():
        text = f"{text} • Remote".strip(" •")
    return text


def _fetch_workable_v1(account: str) -> list[dict]:
    """
    Read a Workable board from the v1 widget endpoint.

    Preferred over v3: it returns every posting in one response (verified 33/33
    against a board v3 reported as total=33), needs no pagination, and is rate
    limited separately - v3 was returning 429 while this kept answering 200.
    """
    url = WORKABLE_V1_URL.format(account=account)
    response = _request_with_retry("GET", url, headers=REQUEST_HEADERS)
    if response.status_code == 404:
        raise ValueError(f"Workable account '{account}' not found (404). Check the subdomain.")
    response.raise_for_status()
    out = []
    for job in response.json().get("jobs", []):
        out.append({
            "title": job.get("title", ""),
            "url": job.get("url") or job.get("shortlink") or job.get("application_url", ""),
            "location_text": _workable_location_text(
                job.get("city"), job.get("state"), job.get("country"),
                job.get("locations"), job.get("telecommuting"),
            ),
        })
    return out


def _fetch_workable_v3(account: str) -> list[dict]:
    """
    Read a Workable board from the v3 endpoint, as a fallback to v1.

    v3 is a POST search (a GET 404s) and pages 10 at a time behind a "nextPage"
    token passed back as {"token": ...}; without following it this returns 10 of
    33 jobs and still looks like it worked.
    """
    url = WORKABLE_V3_URL.format(account=account)
    out, token = [], None
    for _page in range(WORKABLE_MAX_PAGES):
        payload = {"token": token} if token else {}
        response = _request_with_retry("POST", url, json=payload, headers=REQUEST_HEADERS)
        if response.status_code == 404:
            raise ValueError(f"Workable account '{account}' not found (404). Check the subdomain.")
        response.raise_for_status()
        body = response.json()
        for job in body.get("results", []):
            location = job.get("location") or {}
            shortcode = job.get("shortcode", "")
            out.append({
                "title": job.get("title", ""),
                "url": f"https://apply.workable.com/{account}/j/{shortcode}/" if shortcode else "",
                "location_text": location.get("display") or _workable_location_text(
                    location.get("city"), location.get("region"), location.get("country"),
                    job.get("locations"),
                    job.get("remote") or str(job.get("workplace", "")).lower() == "remote",
                ),
            })
        token = body.get("nextPage")
        if not token:
            break
        time.sleep(WORKABLE_PAGE_DELAY_SECONDS)
    return out


def fetch_workable_jobs(
    account: str, keywords: list[str] | None = None,
    exclude_re=SENIORITY_EXCLUDE_RE,
) -> list[dict]:
    """Return Workable postings matching keywords + location, via v1 then v3."""
    keywords = keywords or DEFAULT_KEYWORDS
    try:
        postings = _fetch_workable_v1(account)
    except RateLimitedError:
        # The two endpoints are limited independently, so a 429 on one is worth
        # retrying on the other before declaring the board unreachable.
        postings = _fetch_workable_v3(account)

    matches = []
    for job in postings:
        if not _title_matches_keywords(job["title"], keywords, exclude_re):
            continue
        if not matches_location(job["location_text"]):
            continue
        matches.append({
            "title": job["title"],
            "url": job["url"],
            "company": account,
            "location": job["location_text"],
            "source": "workable",
        })
    return matches


def scrape_target_companies(
    companies: dict[str, list[str]] | None = None,
    keywords: list[str] | None = None,
    exclude_re=SENIORITY_EXCLUDE_RE,
) -> list[dict]:
    """
    Scrape every board in `companies` - Ashby, Greenhouse, Lever, Workable -
    (defaults to
    TARGET_COMPANIES), applying the keyword + US-remote filters to each.
    A failure on one board (404, network error, etc.) is logged and skipped
    rather than aborting the whole run. Returns the combined list of matches;
    does not write to the queue file itself (call save_pending_jobs after).
    """
    companies = companies or TARGET_COMPANIES
    keywords = keywords or DEFAULT_KEYWORDS
    all_jobs = []
    rate_limited: list[str] = []

    for org_name in companies.get("ashby", []):
        try:
            jobs = fetch_ashby_jobs(org_name, keywords, exclude_re)
            print(f"[ashby] {org_name}: {len(jobs)} match(es)")
            all_jobs.extend(jobs)
        except RateLimitedError as exc:
            rate_limited.append(f"ashby/{org_name}")
            print(f"[ashby] RATE-LIMITED, not scraped: {org_name} ({exc})")
        except Exception as exc:
            print(f"[ashby] Skipping {org_name}: {exc}")

    for board_token in companies.get("greenhouse", []):
        try:
            jobs = fetch_greenhouse_jobs(board_token, keywords, exclude_re)
            print(f"[greenhouse] {board_token}: {len(jobs)} match(es)")
            all_jobs.extend(jobs)
        except RateLimitedError as exc:
            rate_limited.append(f"greenhouse/{board_token}")
            print(f"[greenhouse] RATE-LIMITED, not scraped: {board_token} ({exc})")
        except Exception as exc:
            print(f"[greenhouse] Skipping {board_token}: {exc}")

    for org_name in companies.get("lever", []):
        try:
            jobs = fetch_lever_jobs(org_name, keywords, exclude_re)
            print(f"[lever] {org_name}: {len(jobs)} match(es)")
            all_jobs.extend(jobs)
        except RateLimitedError as exc:
            rate_limited.append(f"lever/{org_name}")
            print(f"[lever] RATE-LIMITED, not scraped: {org_name} ({exc})")
        except Exception as exc:
            print(f"[lever] Skipping {org_name}: {exc}")

    for account in companies.get("workable", []):
        try:
            jobs = fetch_workable_jobs(account, keywords, exclude_re)
            print(f"[workable] {account}: {len(jobs)} match(es)")
            all_jobs.extend(jobs)
        except RateLimitedError as exc:
            rate_limited.append(f"workable/{account}")
            print(f"[workable] RATE-LIMITED, not scraped: {account} ({exc})")
        except Exception as exc:
            print(f"[workable] Skipping {account}: {exc}")

    if rate_limited:
        # Never let this land as a quiet line among 90 boards: a rate-limited
        # board reports zero matches for the same reason an empty one does, and
        # only one of those means "nothing to apply to".
        print(f"\n[warn] {len(rate_limited)} board(s) were NOT scraped because the API "
              f"rate-limited us. These are not 'no openings' - re-run them later:")
        for name in rate_limited:
            print(f"    - {name}")

    return all_jobs


def save_pending_jobs(jobs: list[dict], output_path: Path | None = None) -> None:
    """
    Append newly discovered jobs to a queue file, deduping by URL.

    output_path defaults to queues/pending_jobs.json. Passing a different file
    keeps separate searches apart: a design search run for someone else must
    not land in the queue that batch_runner.py is about to apply to.
    """
    output_path = Path(output_path) if output_path else PENDING_JOBS_PATH
    if not output_path.is_absolute():
        output_path = BASE_DIR / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []

    for job in existing + jobs:
        if isinstance(job.get("url"), str):
            job["url"] = job["url"].strip().rstrip("~")

    existing_urls = {job.get("url") for job in existing}
    new_jobs = [job for job in jobs if job.get("url") and job["url"] not in existing_urls]

    all_jobs = existing + new_jobs
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, indent=2)

    try:
        shown = output_path.relative_to(BASE_DIR)
    except ValueError:
        shown = output_path  # queue written outside the repo
    print(f"Saved {len(new_jobs)} new job(s) to {shown} ({len(all_jobs)} total).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scraper.py",
        description="Discover job URLs from Greenhouse, Ashby, Lever and Workable boards into a queue file.",
        epilog=(
            "Examples:\n"
            "  scraper.py all \"Data Scientist\" \"Python\"\n"
            "  scraper.py greenhouse stripe Backend\n"
            "  # a separate design search that leaves your own queue untouched:\n"
            "  scraper.py all --roles \"Graphic Designer, Visual Designer\" \\\n"
            "      --output queues/design_jobs.json --allow-senior\n"
            "  # keep Senior but still drop Director/VP:\n"
            "  scraper.py all --roles \"Visual Designer\" --output queues/design_jobs.json \\\n"
            "      --exclude-titles \"director|vp|head of|chief\"\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source", choices=["greenhouse", "ashby", "lever", "workable", "all"]
    )
    parser.add_argument(
        "rest", nargs="*",
        help="For greenhouse/ashby: board token or org name, then keywords. "
             "For all: keywords. Ignored if --roles is given.",
    )
    parser.add_argument(
        "--roles", default="",
        help="Comma-separated role keywords, as an alternative to positional ones.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Queue file to append to (default queues/pending_jobs.json). Use a "
             "separate file to keep one person's search out of another's queue.",
    )
    seniority = parser.add_mutually_exclusive_group()
    seniority.add_argument(
        "--allow-senior", action="store_true",
        help="Keep senior/lead/staff titles. The default filter is tuned for a "
             "junior engineering search and would drop 'Senior Graphic Designer'.",
    )
    seniority.add_argument(
        "--exclude-titles", default=None, metavar="REGEX",
        help="Custom seniority pattern, replacing the default one.",
    )
    return parser


def main():
    args = build_parser().parse_args()

    if args.roles.strip():
        keywords = [r.strip() for r in args.roles.split(",") if r.strip()]
        rest = list(args.rest)
    else:
        rest = list(args.rest)
        keywords = []

    if args.allow_senior:
        exclude_re = None
    elif args.exclude_titles:
        try:
            exclude_re = re.compile(args.exclude_titles, re.IGNORECASE)
        except re.error as exc:
            print(f"--exclude-titles is not a valid regex: {exc}")
            sys.exit(2)
    else:
        exclude_re = SENIORITY_EXCLUDE_RE

    if args.source == "all":
        keywords = keywords or rest or DEFAULT_KEYWORDS
        jobs = scrape_target_companies(keywords=keywords, exclude_re=exclude_re)
        print(f"Total US/remote jobs matched across all boards: {len(jobs)}")
        save_pending_jobs(jobs, args.output)
        return

    if not rest:
        print(f"{args.source} needs a board token or org name. "
              f"Example: scraper.py {args.source} stripe")
        sys.exit(1)
    identifier, *positional_keywords = rest
    keywords = keywords or positional_keywords or DEFAULT_KEYWORDS

    fetchers = {
        "greenhouse": fetch_greenhouse_jobs,
        "ashby": fetch_ashby_jobs,
        "lever": fetch_lever_jobs,
        "workable": fetch_workable_jobs,
    }
    jobs = fetchers[args.source](identifier, keywords, exclude_re)

    print(f"Found {len(jobs)} matching job(s) for '{identifier}' on {args.source}.")
    save_pending_jobs(jobs, args.output)


if __name__ == "__main__":
    main()
