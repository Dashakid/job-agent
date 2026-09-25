"""
One text-generation entry point for drafting, with free-tier fallback.

Providers are tried in order and the first that answers wins, so a daily quota
on one (Gemini's free tier allows 20 requests/day on gemini-2.5-flash) falls
through to the next instead of leaving drafts blank:

    groq    GROQ_API_KEY    gpt-oss-120b on Groq's free tier (far higher daily cap)
    gemini  GEMINI_API_KEY  gemini-2.5-flash

Set LLM_PROVIDER=gemini (or groq) to try that provider first. Groq speaks the
OpenAI chat API, so it is called with requests and needs no extra package.
"""

import os
import re
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq retires models often; set GROQ_MODEL to pick another from its /models list.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MAX_ATTEMPTS = 4
GROQ_MAX_WAIT_SECONDS = 65
GEMINI_MODEL = "gemini-2.5-flash"
PROVIDER_KEYS = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}
DEFAULT_ORDER = ("groq", "gemini")


class LLMUnavailable(RuntimeError):
    """No provider is configured, or every configured provider failed."""


def available_providers() -> list[str]:
    """Configured providers in the order they will be tried."""
    order = list(DEFAULT_ORDER)
    preferred = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return [name for name in order if os.environ.get(PROVIDER_KEYS[name])]


def _retry_after_seconds(response: requests.Response) -> float:
    """Seconds Groq asks us to wait: the retry-after header, else 'try again in 12.3s'."""
    header = response.headers.get("retry-after", "")
    try:
        return float(header)
    except ValueError:
        match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", response.text)
        if match:
            return int(match.group(1) or 0) * 60 + float(match.group(2))
    return 10.0


def _generate_groq(prompt: str) -> str:
    # The free tier caps tokens per minute (8,000 on gpt-oss-120b), roughly two
    # drafting prompts, so a 429 usually clears within a minute: wait it out
    # rather than falling through to a provider whose daily quota may be gone.
    for attempt in range(GROQ_MAX_ATTEMPTS):
        response = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.7},
            timeout=60,
        )
        if response.status_code == 429 and attempt < GROQ_MAX_ATTEMPTS - 1:
            wait = min(_retry_after_seconds(response), GROQ_MAX_WAIT_SECONDS) + 1
            print(f"  [llm] Groq rate limit; waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Groq {response.status_code}: {response.text[:200]}")
        return response.json()["choices"][0]["message"]["content"] or ""
    raise RuntimeError("Groq rate limit did not clear")


def _generate_gemini(prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text or ""


_GENERATORS = {"groq": _generate_groq, "gemini": _generate_gemini}


def generate(prompt: str) -> str:
    """Return the first provider's answer, falling through on quota or network errors."""
    providers = available_providers()
    if not providers:
        raise LLMUnavailable("no drafting key set (GROQ_API_KEY or GEMINI_API_KEY)")
    errors = []
    for name in providers:
        try:
            return _GENERATORS[name](prompt).strip()
        except Exception as error:
            errors.append(f"{name}: {str(error)[:160]}")
            if len(providers) > 1:
                print(f"  [llm] {name} unavailable, trying the next provider")
    raise LLMUnavailable("; ".join(errors))
