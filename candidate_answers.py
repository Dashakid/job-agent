"""Grounded drafting for open-ended job application questions."""

import json
import re
from pathlib import Path

import llm

BASE_DIR = Path(__file__).resolve().parent
CANDIDATE_CONTEXT_PATH = BASE_DIR / "candidate_context.md"
# Pre-written answers to recurring prompts; matched before any model call.
PREPARED_ANSWERS_PATH = BASE_DIR / "prepared_answers.json"
GEMINI_MODEL = llm.GEMINI_MODEL

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


def load_prepared_answers() -> list[tuple[re.Pattern, str]]:
    """Load the applicant's pre-written answers as (compiled pattern, answer) pairs."""
    try:
        rules = json.loads(PREPARED_ANSWERS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    if not isinstance(rules, list):
        return []
    compiled = []
    for rule in rules:
        pattern, answer = rule.get("pattern", ""), rule.get("answer", "")
        if pattern and answer:
            compiled.append((re.compile(pattern, re.IGNORECASE), answer))
    return compiled


def match_prepared_answer(question: str, rules: list[tuple[re.Pattern, str]]) -> str | None:
    """Return the first prepared answer whose pattern matches the question label."""
    for pattern, answer in rules:
        if pattern.search(question):
            return answer
    return None


def draft_answers(questions: list[str], job_context: str = "") -> list[str | None]:
    """Answer questions in order: prepared answers first, then grounded model drafts.

    Returns None for any question with neither a prepared answer nor a grounded draft.
    """
    if not questions:
        return []
    rules = load_prepared_answers()
    results: list[str | None] = [match_prepared_answer(q, rules) for q in questions]
    pending = [index for index, answer in enumerate(results) if answer is None]
    if not pending:
        return results
    drafted = _draft_with_llm([questions[index] for index in pending], job_context)
    for index, answer in zip(pending, drafted):
        results[index] = answer
    return results


def _draft_with_llm(questions: list[str], job_context: str) -> list[str | None]:
    """Draft grounded answers in question order, returning None when facts are insufficient."""
    if not llm.available_providers():
        print("  [draft] No drafting key set (GROQ_API_KEY or GEMINI_API_KEY); "
              "open-ended fields remain for review.")
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
        raw_text = llm.generate(prompt)
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text)
        answers = json.loads(raw_text)
        if not isinstance(answers, list) or len(answers) != len(questions):
            raise ValueError("drafting model returned an unexpected answer count")
        return [answer.strip() if isinstance(answer, str) and answer.strip() else None for answer in answers]
    except Exception as error:
        print(f"  [draft] Answer drafting unavailable: {error}")
        return [None] * len(questions)
