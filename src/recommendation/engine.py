from __future__ import annotations

"""Recommendation engine: generates a structured investment recommendation.

Takes accumulated extractions, final memo, and gap analyses from all calls
and produces an INVEST / PASS / REVISIT recommendation with a scored rubric,
confidence score, and rationale.

CLI:
    python -m src.recommendation.engine --extractions data/output/lazo/extraction_call1.json data/output/lazo/extraction_call2.json data/output/lazo/extraction_call3.json --memo data/output/lazo/memo_v3.md
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.recommendation.prompts import RECOMMENDATION_SYSTEM_PROMPT

load_dotenv()

MODEL = "claude-sonnet-4-20250514"

RUBRIC_DIMENSIONS = ["team", "market", "product", "business_model", "traction", "competition"]


def generate_recommendation(
    extractions: list[dict],
    memo: str,
    gap_analyses: list[dict] | None = None,
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Generate a structured investment recommendation.

    Args:
        extractions: All accumulated extraction dicts (one per call).
        memo: Final memo markdown string.
        gap_analyses: Gap analysis dicts from each call (optional).
        client: Optional shared Anthropic client.

    Returns:
        Dict with recommendation, confidence_score, rubric, overall_rationale, overall_score.
    """
    if client is None:
        client = anthropic.Anthropic()

    # Build user message with all data
    parts = []

    for ext in extractions:
        stage = ext.get("call_stage", "?")
        parts.append(
            f"## EXTRACTION (Call {stage})\n\n"
            f"{json.dumps(ext, indent=2, ensure_ascii=False)}"
        )

    parts.append(f"## INVESTMENT MEMO\n\n{memo}")

    if gap_analyses:
        for gap in gap_analyses:
            stage = gap.get("call_stage", "?")
            parts.append(
                f"## GAP ANALYSIS (Call {stage})\n\n"
                f"{json.dumps(gap, indent=2, ensure_ascii=False)}"
            )

    parts.append(
        "Based on ALL the data above, produce your investment recommendation "
        "with the scored rubric and rationale. Return only the JSON object."
    )

    user_message = "\n\n".join(parts)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=RECOMMENDATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    response_text = response.content[0].text.strip()

    # Strip markdown fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        response_text = "\n".join(lines[start:end])

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        # Retry once with feedback
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=RECOMMENDATION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a valid JSON object, no extra text."},
            ],
        )
        response_text = retry_response.content[0].text.strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            start = 1 if lines[0].startswith("```") else 0
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            response_text = "\n".join(lines[start:end])
        result = json.loads(response_text)

    # Compute derived fields
    rubric = result.get("rubric", {})
    scores = [rubric[dim]["score"] for dim in RUBRIC_DIMENSIONS if dim in rubric]
    overall_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    result["overall_score"] = overall_score

    # Compute confidence score from dimension scores
    # Higher average + fewer gaps in data = higher confidence
    result["confidence_score"] = _compute_confidence(overall_score, rubric, gap_analyses)

    return result


def _compute_confidence(
    overall_score: float,
    rubric: dict,
    gap_analyses: list[dict] | None,
) -> int:
    """Compute a confidence score (0-100) based on rubric and data completeness.

    Factors:
    - Base from score spread: tighter spread = higher confidence
    - Penalty for remaining gaps (unanswered questions)
    - Bonus for having all 3 calls worth of data
    """
    scores = [rubric[dim]["score"] for dim in RUBRIC_DIMENSIONS if dim in rubric]
    if not scores:
        return 0

    # Start at 70 for having a complete 3-call evaluation
    confidence = 70

    # Score spread penalty: stdev > 1.0 reduces confidence
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    stdev = variance ** 0.5
    if stdev > 1.5:
        confidence -= 15
    elif stdev > 1.0:
        confidence -= 10
    elif stdev < 0.5:
        confidence += 5

    # Penalty for remaining gaps
    if gap_analyses:
        total_questions = sum(
            len(g.get("follow_up_questions", []))
            for g in gap_analyses
        )
        if total_questions > 20:
            confidence -= 15
        elif total_questions > 10:
            confidence -= 10
        elif total_questions > 5:
            confidence -= 5

    # Extreme scores reduce confidence (very high or very low are easier to call)
    if mean >= 4.0 or mean <= 2.0:
        confidence += 10
    elif 2.5 <= mean <= 3.5:
        confidence -= 5  # Middle ground is harder to call

    return max(0, min(100, confidence))


def main():
    parser = argparse.ArgumentParser(
        description="Generate an investment recommendation from extractions and memo."
    )
    parser.add_argument(
        "--extractions",
        nargs="+",
        required=True,
        help="Paths to extraction JSON files (one per call)",
    )
    parser.add_argument(
        "--memo",
        required=True,
        help="Path to the final memo .md file",
    )
    parser.add_argument(
        "--gap-analyses",
        nargs="*",
        help="Paths to gap analysis JSON files",
    )
    parser.add_argument(
        "--output",
        help="Path to write recommendation JSON (prints to stdout if omitted)",
    )

    args = parser.parse_args()

    # Load extractions
    extractions = []
    for ext_path_str in args.extractions:
        ext_path = Path(ext_path_str)
        if not ext_path.exists():
            print(f"Error: Extraction not found: {ext_path}", file=sys.stderr)
            sys.exit(1)
        extractions.append(json.loads(ext_path.read_text()))
    print(f"Loaded {len(extractions)} extraction(s)", file=sys.stderr)

    # Load memo
    memo_path = Path(args.memo)
    if not memo_path.exists():
        print(f"Error: Memo not found: {memo_path}", file=sys.stderr)
        sys.exit(1)
    memo = memo_path.read_text()
    print(f"Loaded memo ({len(memo)} chars)", file=sys.stderr)

    # Load gap analyses
    gap_analyses = None
    if args.gap_analyses:
        gap_analyses = []
        for gap_path_str in args.gap_analyses:
            gap_path = Path(gap_path_str)
            if not gap_path.exists():
                print(f"Error: Gap analysis not found: {gap_path}", file=sys.stderr)
                sys.exit(1)
            gap_analyses.append(json.loads(gap_path.read_text()))
        print(f"Loaded {len(gap_analyses)} gap analysis(es)", file=sys.stderr)

    print("Generating recommendation...", file=sys.stderr)
    recommendation = generate_recommendation(extractions, memo, gap_analyses)

    output_json = json.dumps(recommendation, indent=2, ensure_ascii=False)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_json)
        print(f"Recommendation saved to {output_path}", file=sys.stderr)
    else:
        print(output_json)

    # Print summary
    print(f"\nRecommendation: {recommendation['recommendation']}", file=sys.stderr)
    print(f"Confidence: {recommendation['confidence_score']}%", file=sys.stderr)
    print(f"Overall score: {recommendation['overall_score']}/5", file=sys.stderr)
    for dim in RUBRIC_DIMENSIONS:
        if dim in recommendation.get("rubric", {}):
            score = recommendation["rubric"][dim]["score"]
            print(f"  {dim}: {score}/5", file=sys.stderr)


if __name__ == "__main__":
    main()
