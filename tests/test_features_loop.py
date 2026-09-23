import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import cli
import main
import scraper


def test_1_queue_normalization():
    """Verify trailing tildes and whitespace are stripped during queue persistence."""
    print("[TEST 1/4] Running Queue Normalization & Sanitization Test...")
    with tempfile.TemporaryDirectory(dir=scraper.BASE_DIR) as tmpdir:
        queue_path = Path(tmpdir) / "pending_jobs.json"
        queue_path.write_text(
            json.dumps([{"url": "https://example.com/job1~"}]), encoding="utf-8"
        )
        incoming = [
            {"url": " https://example.com/job1~ "},
            {"url": "https://example.com/job2~~~"},
        ]

        with patch.object(scraper, "PENDING_JOBS_PATH", queue_path):
            scraper.save_pending_jobs(incoming)

        saved = json.loads(queue_path.read_text(encoding="utf-8"))
        urls = [job["url"] for job in saved]
        assert urls == ["https://example.com/job1", "https://example.com/job2"], (
            f"Unexpected URLs: {urls}"
        )
    print(" -> PASSED: Queue successfully normalizes and deduplicates URLs.\n")


def test_2_cli_routing():
    """Verify main.py delegates to cli.py and the apply arguments parse correctly."""
    print("[TEST 2/4] Running CLI Routing Test...")
    parser = cli.build_parser()
    parsed = parser.parse_args(["apply", "--url", "https://example.com/test-job"])
    assert parsed.command == "apply"
    assert parsed.url == "https://example.com/test-job"

    with patch("cli.main", return_value=17) as cli_main:
        assert main.main() == 17
        cli_main.assert_called_once_with()
    print(" -> PASSED: CLI arguments route and parse as expected.\n")


def test_3_self_healing_retry_and_fallback():
    """Verify retry count, diagnostics, and the manual safety fallback."""
    print("[TEST 3/4] Running Self-Healing Retry & Fallback Test...")
    fake_page = MagicMock()
    call_count = 0

    def failing_action(_page):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(f"Simulated failure attempt {call_count}")

    with (
        patch("main._diagnose_failure", return_value="Mock Gemini suggestion") as diagnose,
        patch("main._log_self_heal_event") as log_event,
        patch("main.time.sleep"),
        patch("builtins.input", return_value="") as review_pause,
    ):
        result = main.self_heal_action(
            fake_page,
            "TestStep",
            failing_action,
            "https://example.com/fail",
            max_retries=3,
        )

    assert result is None
    assert call_count == 3, f"Expected 3 retry attempts, got {call_count}"
    assert diagnose.call_count == 3
    assert log_event.call_count == 2
    review_pause.assert_called_once()
    print(" -> PASSED: Self-healing retried and invoked the safety fallback.\n")


def test_4_gemini_missing_key_graceful_handling():
    """Verify diagnostics degrade clearly when GEMINI_API_KEY is absent."""
    print("[TEST 4/4] Running Gemini Graceful Degradation Test...")
    fake_page = MagicMock()
    fake_page.evaluate.return_value = "Mock Form Content"
    clean_environment = dict(os.environ)
    clean_environment.pop("GEMINI_API_KEY", None)

    with tempfile.TemporaryDirectory(dir=main.BASE_DIR) as tmpdir:
        with (
            patch.dict(os.environ, clean_environment, clear=True),
            patch.object(main, "FAILURE_SNAPSHOT_DIR", Path(tmpdir)),
        ):
            suggestion = main._diagnose_failure(
                fake_page, "TestStep", RuntimeError("Error")
            )

    assert "GEMINI_API_KEY is not set" in suggestion, (
        f"Unexpected suggestion: {suggestion}"
    )
    print(" -> PASSED: Missing API key handled gracefully without traceback.\n")


def run_feature_loop(iterations=3):
    """Run all feature verifications repeatedly."""
    print("\n==============================================")
    print(f" STARTING FEATURE VERIFICATION LOOP ({iterations} iterations)")
    print("==============================================\n")

    for iteration in range(1, iterations + 1):
        print(f"--- Iteration {iteration} of {iterations} ---")
        test_1_queue_normalization()
        test_2_cli_routing()
        test_3_self_healing_retry_and_fallback()
        test_4_gemini_missing_key_graceful_handling()
        if iteration < iterations:
            time.sleep(0.5)

    print("==============================================")
    print(" ALL ITERATIONS COMPLETED SUCCESSFULLY")
    print("==============================================")


if __name__ == "__main__":
    run_feature_loop(iterations=3)
