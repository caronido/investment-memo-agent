from __future__ import annotations

"""Initial evaluation engine: pre-call deck screening.

Analyzes a pitch deck before any founder calls to produce:
1. A WORTH_CALL / NOT_WORTH_CALL / NEEDS_MORE_INFO recommendation with 4-dimension rubric
2. 10 specific questions for Call 1

CLI:
    python -m src.initial_evaluation.evaluator --pdf data/documents/deck.pdf --output-dir data/output/lazo/
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.ingestion.document_processor import extract_from_document
from src.initial_evaluation.prompts import (
    INITIAL_QUESTIONS_SYSTEM_PROMPT,
    INITIAL_RECOMMENDATION_SYSTEM_PROMPT,
)

load_dotenv()

MODEL = "claude-sonnet-4-20250514"

RUBRIC_DIMENSIONS = ["team", "market", "product", "business_model"]


def run_initial_evaluation(
    pdf_path: str | Path,
    *,
    client: anthropic.Anthropic | None = None,
    output_dir: str | Path | None = None,
) -> dict:
    """Run the full initial evaluation pipeline on a pitch deck.

    Steps:
        1. Extract structured data from the PDF (reuses document_processor)
        2. Generate initial recommendation (WORTH_CALL / NOT_WORTH_CALL / NEEDS_MORE_INFO)
        3. Generate 10 targeted questions for Call 1

    Args:
        pdf_path: Path to the pitch deck PDF.
        client: Optional shared Anthropic client.
        output_dir: Directory to save output files.

    Returns:
        Dict with keys: extraction, recommendation, questions, pdf_path.
    """
    if client is None:
        client = anthropic.Anthropic()

    pdf_path = Path(pdf_path)
    print(f"Extracting from deck: {pdf_path.name}", file=sys.stderr)

    # 1. Extract structured data from document
    extraction = extract_from_document(pdf_path, call_stage=1, client=client)
    print(f"Extraction complete: {_count_fields(extraction)} fields", file=sys.stderr)

    # 2. Generate initial recommendation
    print("Generating initial recommendation...", file=sys.stderr)
    recommendation = generate_initial_recommendation(extraction, client=client)
    print(
        f"Recommendation: {recommendation['recommendation']} "
        f"(score: {recommendation['overall_score']}/5, "
        f"confidence: {recommendation['confidence_score']}%)",
        file=sys.stderr,
    )

    # 3. Generate questions for Call 1
    print("Generating Call 1 questions...", file=sys.stderr)
    questions = generate_initial_questions(extraction, client=client)
    print(f"Generated {len(questions.get('questions', []))} questions", file=sys.stderr)

    # Save outputs
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        (output_dir / "initial_extraction.json").write_text(
            json.dumps(extraction, indent=2, ensure_ascii=False)
        )
        (output_dir / "initial_recommendation.json").write_text(
            json.dumps(recommendation, indent=2, ensure_ascii=False)
        )
        (output_dir / "initial_questions.json").write_text(
            json.dumps(questions, indent=2, ensure_ascii=False)
        )
        print(f"Outputs saved to {output_dir}", file=sys.stderr)

    return {
        "extraction": extraction,
        "recommendation": recommendation,
        "questions": questions,
        "pdf_path": str(pdf_path),
    }


def generate_initial_recommendation(
    extraction: dict,
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Generate a pre-call recommendation from deck extraction.

    Args:
        extraction: Structured extraction from the pitch deck.
        client: Optional shared Anthropic client.

    Returns:
        Dict with recommendation, rubric (4 dims), overall_score,
        confidence_score, overall_rationale, key_risks.
    """
    if client is None:
        client = anthropic.Anthropic()

    user_message = (
        "## PITCH DECK EXTRACTION\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}\n\n"
        "Based on the deck extraction above, produce your initial screening "
        "recommendation with the scored rubric, rationale, and key risks. "
        "Return only the JSON object."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=INITIAL_RECOMMENDATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    response_text = response.content[0].text.strip()
    response_text = _strip_markdown_fences(response_text)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        # Retry once with feedback
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=INITIAL_RECOMMENDATION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a valid JSON object, no extra text."},
            ],
        )
        response_text = _strip_markdown_fences(retry_response.content[0].text.strip())
        result = json.loads(response_text)

    # Compute derived fields
    rubric = result.get("rubric", {})
    scores = [rubric[dim]["score"] for dim in RUBRIC_DIMENSIONS if dim in rubric]
    overall_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    result["overall_score"] = overall_score

    result["confidence_score"] = _compute_initial_confidence(overall_score, rubric)

    return result


def generate_initial_questions(
    extraction: dict,
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Generate 10 targeted questions for Call 1 based on deck extraction.

    Args:
        extraction: Structured extraction from the pitch deck.
        client: Optional shared Anthropic client.

    Returns:
        Dict with questions list (10 items, each with question/category/rationale).
    """
    if client is None:
        client = anthropic.Anthropic()

    user_message = (
        "## PITCH DECK EXTRACTION\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}\n\n"
        "Based on the deck extraction above, generate exactly 10 specific "
        "questions for Call 1 (Founder Story). Return only the JSON object."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=INITIAL_QUESTIONS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    response_text = response.content[0].text.strip()
    response_text = _strip_markdown_fences(response_text)

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=INITIAL_QUESTIONS_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a valid JSON object, no extra text."},
            ],
        )
        response_text = _strip_markdown_fences(retry_response.content[0].text.strip())
        result = json.loads(response_text)

    return result


def _compute_initial_confidence(overall_score: float, rubric: dict) -> int:
    """Compute a confidence score (0-100) for deck-only evaluation.

    Lower base than full recommendation (50 vs 70) since deck-only data
    has inherently less signal.

    Factors:
    - Base 50 (deck-only, no founder conversations)
    - Score spread penalty: high variance reduces confidence
    - Completeness bonus: all 4 dimensions scored
    - Extremes bonus: very high/low scores are easier to call
    """
    scores = [rubric[dim]["score"] for dim in RUBRIC_DIMENSIONS if dim in rubric]
    if not scores:
        return 0

    confidence = 50

    # Completeness: bonus if all 4 dimensions scored
    if len(scores) == len(RUBRIC_DIMENSIONS):
        confidence += 5

    # Score spread penalty
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    stdev = variance ** 0.5
    if stdev > 1.5:
        confidence -= 15
    elif stdev > 1.0:
        confidence -= 10
    elif stdev < 0.5:
        confidence += 5

    # Extreme scores are easier to call
    if mean >= 4.0 or mean <= 2.0:
        confidence += 10
    elif 2.5 <= mean <= 3.5:
        confidence -= 5

    return max(0, min(100, confidence))


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from response text."""
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end])
    return text


def _count_fields(extraction: dict) -> int:
    """Count non-null leaf fields in an extraction."""
    count = 0
    for key, val in extraction.items():
        if key.startswith("_") or key in ("sources", "call_stage"):
            continue
        if isinstance(val, dict):
            for _, v in val.items():
                if v is not None and v != "" and v != []:
                    count += 1
        elif isinstance(val, list):
            count += len(val)
        elif val is not None and val != "":
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Run initial evaluation on a pitch deck (pre-call screening)."
    )
    parser.add_argument(
        "--pdf",
        required=True,
        help="Path to the pitch deck PDF",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to save output files",
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    result = run_initial_evaluation(
        pdf_path,
        output_dir=args.output_dir,
    )

    # Print summary
    rec = result["recommendation"]
    print(f"\nRecommendation: {rec['recommendation']}", file=sys.stderr)
    print(f"Confidence: {rec['confidence_score']}%", file=sys.stderr)
    print(f"Overall score: {rec['overall_score']}/5", file=sys.stderr)
    for dim in RUBRIC_DIMENSIONS:
        if dim in rec.get("rubric", {}):
            score = rec["rubric"][dim]["score"]
            print(f"  {dim}: {score}/5", file=sys.stderr)
    print(f"\nKey risks:", file=sys.stderr)
    for risk in rec.get("key_risks", []):
        print(f"  - {risk}", file=sys.stderr)
    print(f"\nQuestions for Call 1:", file=sys.stderr)
    for i, q in enumerate(result["questions"].get("questions", []), 1):
        print(f"  {i}. [{q['category']}] {q['question']}", file=sys.stderr)


if __name__ == "__main__":
    main()
