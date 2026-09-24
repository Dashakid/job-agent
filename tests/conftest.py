import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_providers(monkeypatch):
    """Keep tests off real APIs: only a test's own patched GEMINI_API_KEY is visible."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
