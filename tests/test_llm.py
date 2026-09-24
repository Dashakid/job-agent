import os
import unittest
from unittest.mock import MagicMock, patch

import llm


class ProviderOrderTests(unittest.TestCase):
    def test_groq_first_when_both_keys_set(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}, clear=True):
            self.assertEqual(llm.available_providers(), ["groq", "gemini"])

    def test_llm_provider_moves_one_to_front(self):
        env = {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m", "LLM_PROVIDER": "gemini"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(llm.available_providers(), ["gemini", "groq"])

    def test_no_keys_raises(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(llm.LLMUnavailable):
            llm.generate("hi")


class FallbackTests(unittest.TestCase):
    def test_quota_error_falls_through_to_next_provider(self):
        failing = MagicMock(side_effect=RuntimeError("429 quota"))
        working = MagicMock(return_value="  drafted  ")
        with patch.dict(os.environ, {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}, clear=True), \
                patch.dict(llm._GENERATORS, {"groq": failing, "gemini": working}):
            self.assertEqual(llm.generate("prompt"), "drafted")
        failing.assert_called_once_with("prompt")

    def test_all_failing_raises_with_every_error(self):
        failing = MagicMock(side_effect=RuntimeError("down"))
        with patch.dict(os.environ, {"GROQ_API_KEY": "g", "GEMINI_API_KEY": "m"}, clear=True), \
                patch.dict(llm._GENERATORS, {"groq": failing, "gemini": failing}):
            with self.assertRaises(llm.LLMUnavailable) as caught:
                llm.generate("prompt")
        self.assertIn("groq: down", str(caught.exception))
        self.assertIn("gemini: down", str(caught.exception))

    def test_groq_request_shape(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "hello"}}]}
        with patch.dict(os.environ, {"GROQ_API_KEY": "secret"}, clear=True), \
                patch.object(llm.requests, "post", return_value=response) as post:
            self.assertEqual(llm.generate("prompt"), "hello")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["json"]["model"], llm.GROQ_MODEL)

    def test_groq_waits_out_rate_limit_then_succeeds(self):
        limited = MagicMock(status_code=429, headers={"retry-after": "2"}, text="")
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": "done"}}]}
        with patch.dict(os.environ, {"GROQ_API_KEY": "k"}, clear=True), \
                patch.object(llm.requests, "post", side_effect=[limited, ok]), \
                patch.object(llm.time, "sleep") as sleep:
            self.assertEqual(llm.generate("prompt"), "done")
        sleep.assert_called_once_with(3.0)

    def test_retry_after_parsed_from_message(self):
        response = MagicMock(headers={}, text="Please try again in 1m2.5s.")
        self.assertEqual(llm._retry_after_seconds(response), 62.5)


if __name__ == "__main__":
    unittest.main()
