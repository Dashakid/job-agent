import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_answers


class CandidateAnswerTests(unittest.TestCase):
    def test_context_is_loaded_at_runtime(self):
        with tempfile.TemporaryDirectory(dir=candidate_answers.BASE_DIR) as tmpdir:
            context_path = Path(tmpdir) / "candidate_context.md"
            context_path.write_text("first version", encoding="utf-8")
            with patch.object(candidate_answers, "CANDIDATE_CONTEXT_PATH", context_path):
                self.assertEqual(candidate_answers.load_candidate_context(), "first version")
                context_path.write_text("second version", encoding="utf-8")
                self.assertEqual(candidate_answers.load_candidate_context(), "second version")

    def test_sensitive_and_identity_questions_are_excluded(self):
        self.assertTrue(
            candidate_answers.is_eligible_open_question(
                "Why do you want to work here?", "textarea", "textarea"
            )
        )
        for question in (
            "Disability status",
            "Salary expectations",
            "Will you require visa sponsorship?",
            "Gender identity",
            "Employment agreement restrictions",
        ):
            self.assertFalse(
                candidate_answers.is_eligible_open_question(
                    question, "textarea", "textarea"
                )
            )
        self.assertFalse(
            candidate_answers.is_eligible_open_question(
                "GitLab username", "input", "text"
            )
        )

    def test_drafts_parse_in_question_order(self):
        captured = {}

        class FakeModels:
            def generate_content(self, model, contents):
                captured["model"] = model
                captured["contents"] = contents
                return types.SimpleNamespace(
                    text=json.dumps(["Grounded first answer", None])
                )

        class FakeClient:
            def __init__(self, api_key):
                captured["api_key"] = api_key
                self.models = FakeModels()

        fake_genai = types.SimpleNamespace(Client=FakeClient)
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch.dict("sys.modules", {"google": fake_google, "google.genai": fake_genai}),
            patch.object(candidate_answers, "load_candidate_context", return_value="Trusted facts"),
            patch.object(candidate_answers, "PREPARED_ANSWERS_PATH", Path("/nonexistent.json")),
        ):
            answers = candidate_answers.draft_answers(
                ["Why this role?", "Unsupported fact?"], "Example job"
            )

        self.assertEqual(answers, ["Grounded first answer", None])
        self.assertEqual(captured["model"], candidate_answers.GEMINI_MODEL)
        self.assertEqual(captured["api_key"], "test-key")
        self.assertIn("Trusted facts", captured["contents"])


class PreparedAnswerTests(unittest.TestCase):
    def _with_rules(self, rules):
        tmpdir = tempfile.TemporaryDirectory(dir=candidate_answers.BASE_DIR)
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / "prepared_answers.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        patcher = patch.object(candidate_answers, "PREPARED_ANSWERS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prepared_answer_fills_without_gemini_key(self):
        self._with_rules([{"pattern": r"open\s+source", "answer": "https://github.com/example"}])
        with patch.dict(os.environ, {}, clear=True):
            answers = candidate_answers.draft_answers(
                ["Please share links of any open source projects*", "Why this role?"]
            )
        self.assertEqual(answers, ["https://github.com/example", None])

    def test_only_unmatched_questions_reach_gemini(self):
        self._with_rules([{"pattern": r"primary\s+language", "answer": "Python"}])
        with patch.object(
            candidate_answers, "_draft_with_llm", return_value=["Drafted"]
        ) as drafter:
            answers = candidate_answers.draft_answers(
                ["Why this role?", "What is your primary language?"], "ctx"
            )
        drafter.assert_called_once_with(["Why this role?"], "ctx")
        self.assertEqual(answers, ["Drafted", "Python"])

    def test_missing_or_malformed_file_yields_no_rules(self):
        with patch.object(candidate_answers, "PREPARED_ANSWERS_PATH", Path("/nonexistent.json")):
            self.assertEqual(candidate_answers.load_prepared_answers(), [])
        self._with_rules({"not": "a list"})
        self.assertEqual(candidate_answers.load_prepared_answers(), [])


if __name__ == "__main__":
    unittest.main()
