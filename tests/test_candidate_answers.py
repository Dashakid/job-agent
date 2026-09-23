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
        ):
            answers = candidate_answers.draft_answers(
                ["Why this role?", "Unsupported fact?"], "Example job"
            )

        self.assertEqual(answers, ["Grounded first answer", None])
        self.assertEqual(captured["model"], candidate_answers.GEMINI_MODEL)
        self.assertEqual(captured["api_key"], "test-key")
        self.assertIn("Trusted facts", captured["contents"])


if __name__ == "__main__":
    unittest.main()
