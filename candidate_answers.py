"""Grounded drafting for open-ended job application questions."""

import json
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CANDIDATE_CONTEXT_PATH = BASE_DIR / "candidate_context.md"
GEMINI_MODEL = "gemini-2.5-flash"

ESSAY_KEYWORDS = re.compile(
    r"\b(why|describe|tell us|tell me|experience|interest|motivated|motivation|"
    r"challenge|problem|project|accomplishment|achievement|skills|background|"
    r"additional information|anything else|cover letter)\b",
    re.IGNORECASE,
)
SENSITIVE_KEYWORDS = re.compile(
    r"\b(race|ethnicity|hispanic|gender|sex|sexual orientation|pronouns?|"
    r"disability|veteran|religion|age|date of birth|birth date|ssn|social security|"
    r"salary|compensation|criminal|arrest|conviction|medical|health|accommodation|"
    r"employment agreement|non-compete|restriction|visa|sponsorship|citizenship)\b",
    re.IGNORECASE,
)


def load_candidate_context() -> str:
    """Load the candidate knowledge base on every run so edits take effect immediately."""
    if not CANDIDATE_CONTEXT_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CANDIDATE_CONTEXT_PATH.name}. Copy candidate_context.example.md "
            "to candidate_context.md and describe your background."
        )
    return CANDIDATE_CONTEXT_PATH.read_text(encoding="utf-8").strip()


def is_eligible_open_question(label: str, tag_name: str, input_type: str) -> bool:
    """Allow prose questions while excluding disclosures, identity, and sensitive fields."""
    normalized = label.strip()
    if not normalized or SENSITIVE_KEYWORDS.search(normalized):
        return False
    if tag_name.lower() == "textarea":
        return input_type.lower() != "hidden"
    return bool(ESSAY_KEYWORDS.search(normalized))


def draft_answers(questions: list[str], job_context: str = "") -> list[str | None]:
    """Draft grounded answers in question order, returning None when facts are insufficient."""
    if not questions:
        return []
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  [draft] GEMINI_API_KEY is not set; open-ended fields remain for review.")
        return [None] * len(questions)

    try:
        candidate_context = load_candidate_context()
    except FileNotFoundError as error:
        print(f"  [draft] {error} Open-ended fields remain for review.")
        return [None] * len(questions)
    numbered_questions = "\n".join(
        f"{index}. {question}" for index, question in enumerate(questions)
    )
    prompt = f"""You draft job-application responses for manual human review.

TRUSTED CANDIDATE CONTEXT:
{candidate_context}

JOB/PAGE CONTEXT:
{job_context[:4000]}

QUESTIONS:
{numbered_questions}

Return only a JSON array with exactly {len(questions)} entries in the same order.
Each entry must be either a concise first-person answer or null.
Use only facts explicitly present in the trusted candidate context.
Never invent employers, dates, years of experience, metrics, credentials, or personal facts.
Return null for salary, demographic, disability, veteran, legal, criminal, work authorization,
sponsorship, non-compete, or other sensitive/disclosure questions.
Do not include markdown. These are drafts and will be reviewed before submission."""

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        raw_text = (response.text or "").strip()
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text)
        answers = json.loads(raw_text)
        if not isinstance(answers, list) or len(answers) != len(questions):
            raise ValueError("Gemini returned an unexpected answer count")
        return [answer.strip() if isinstance(answer, str) and answer.strip() else None for answer in answers]
    except Exception as error:
        print(f"  [draft] Gemini answer drafting unavailable: {error}")
        return [None] * len(questions)
