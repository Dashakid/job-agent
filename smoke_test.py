"""
Smoke test suite for the job-agent repo's core Python modules.

Runs quick, non-interactive checks (no live browser session) for:
  1. Gemini API connectivity (gemini-2.5-flash)
  2. Local applications_log.json / applications_log.csv I/O (in a temp dir)
  3. Google Sheets service-account credentials (read-only check)
  4. cli.py --help output

Prints a [PASS]/[FAIL] line per test and exits non-zero if any test fails.

Run:
    python smoke_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent
GEMINI_MODEL = "gemini-2.5-flash"


def test_gemini_api() -> tuple[bool, str]:
    """Confirm GEMINI_API_KEY is active by calling gemini-2.5-flash directly."""
    if not os.environ.get("GEMINI_API_KEY"):
        return False, "GEMINI_API_KEY is not set (AI drafting and diagnosis will be disabled)"
    try:
        from google import genai

        client = genai.Client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents="Reply with exactly one word: OK",
        )
        text = (response.text or "").strip()
        if not text:
            return False, "Gemini returned an empty response"
        return True, f"Gemini responded: {text!r}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def test_local_logging() -> tuple[bool, str]:
    """Round-trip a dummy entry through main.log_application() in a temp dir.

    Uses a throwaway directory so running the smoke test never adds fake rows
    to your real applications log (or, via sheets sync, your Google Sheet).
    """
    try:
        import main

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "applications_log.json"
            csv_path = Path(tmpdir) / "applications_log.csv"
            with (
                patch.object(main, "LOG_JSON_PATH", json_path),
                patch.object(main, "LOG_CSV_PATH", csv_path),
                patch.object(main, "sync_logs_to_sheet", lambda: 0),
            ):
                main.log_application("https://example.com/smoke-test", "SmokeTestCo")
            records = json.loads(json_path.read_text(encoding="utf-8"))
            if not csv_path.exists() or records[-1].get("company") != "SmokeTestCo":
                return False, "Log files were not written as expected"
        return True, "log_application() wrote JSON and CSV entries (temp dir)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def test_sheets_credentials() -> tuple[bool, str]:
    """Check Google Sheets credentials without writing anything to the sheet."""
    try:
        import sheets_sync
    except Exception as exc:
        return False, f"Could not import sheets_sync: {type(exc).__name__}: {exc}"
    if not sheets_sync.SERVICE_ACCOUNT_PATH.exists():
        return True, "service_account.json not present; Sheets sync is optional and disabled"
    try:
        sheets_sync._get_client()
        return True, "service_account.json loaded and authorized"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def test_cli_help() -> tuple[bool, str]:
    """Verify cli.py responds to --help without error."""
    try:
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "cli.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return False, f"cli.py --help exited with code {result.returncode}: {result.stderr.strip()}"
        if "usage" not in result.stdout.lower():
            return False, "cli.py --help output missing expected 'usage' text"
        return True, "cli.py --help returned usage output"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    tests = [
        ("Gemini API Test", test_gemini_api),
        ("Local Logging Test", test_local_logging),
        ("Sheets Credentials Test", test_sheets_credentials),
        ("CLI Integration Test", test_cli_help),
    ]

    all_passed = True
    print("=== job-agent smoke test ===\n")
    for name, test_func in tests:
        passed, message = test_func()
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {name}: {message}")
        all_passed = all_passed and passed

    print("\n" + ("All smoke tests passed." if all_passed else "One or more smoke tests failed."))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
