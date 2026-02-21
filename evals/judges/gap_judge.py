"""LLM-as-judge for gap analysis quality evaluation.

Uses Claude Haiku to score gap analysis output on specificity,
stage-appropriateness, and usefulness.
"""

import json

import anthropic

MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator for an SPV network's investment memo pipeline. \
You will be given a structured extraction from a founder call and the gap analysis \
output produced from it (follow-up questions, document requests, coverage assessment).

You also receive the call stage (1, 2, or 3) to understand context:
- After Call 1 (Founder Story): questions should focus on product/tech topics for Call 2
- After Call 2 (Product Deep Dive): questions should focus on GTM/commercial topics for Call 3
- After Call 3 (GTM Validation): questions should cover only remaining open items

Score the gap analysis on three dimensions, each from 1 to 5:

1. **Specificity** (1-5): Are the follow-up questions specific and actionable, \
grounded in the actual extraction data? Do they reference the company name, \
specific numbers, or concrete details from the call?
   - 5: Every question references specific data points, names, or numbers from the extraction
   - 3: Mix of specific and generic questions
   - 1: All questions are generic templates that could apply to any company

2. **Stage-Appropriateness** (1-5): Do the questions match the focus area of the \
NEXT call? After Call 1, are they about product/tech? After Call 2, about GTM/sales?
   - 5: All questions are perfectly aligned with the next call's focus area
   - 3: Most questions match but some belong to a different call stage
   - 1: Questions are mostly about topics already covered or for the wrong stage

3. **Usefulness** (1-5): Would an analyst at an SPV network actually use these \
questions to prepare for the next founder call? Are they the RIGHT questions to ask?
   - 5: These are exactly the questions a sharp analyst would prepare
   - 3: Useful but missing some key questions or including low-value ones
   - 1: Would not help an analyst prepare for the call

Return ONLY a JSON object with this exact structure:
{
  "specificity": {"score": N, "reasoning": "..."},
  "stage_appropriateness": {"score": N, "reasoning": "..."},
  "usefulness": {"score": N, "reasoning": "..."}
}

No markdown fences, no extra text. Just the JSON object."""


def judge_gap_analysis(
    extraction: dict,
    gap_analysis: dict,
    call_stage: int,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Score a gap analysis using Claude Haiku as judge.

    Args:
        extraction: The extraction dict the gap analysis was based on.
        gap_analysis: The gap analysis output to evaluate.
        call_stage: Which call was just completed (1, 2, 3).
        client: Optional Anthropic client.

    Returns:
        Dict with specificity, stage_appropriateness, usefulness scores and
        reasoning, plus an overall_score (average of the three).
    """
    if client is None:
        client = anthropic.Anthropic()

    user_message = (
        f"## CALL STAGE: {call_stage}\n\n"
        "## EXTRACTION\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}\n\n"
        "## GAP ANALYSIS OUTPUT\n\n"
        f"{json.dumps(gap_analysis, indent=2, ensure_ascii=False)}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=JUDGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    response_text = response.content[0].text.strip()

    # Strip markdown fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        response_text = "\n".join(lines[start:end])

    scores = json.loads(response_text)

    # Compute overall score
    dimension_scores = [
        scores["specificity"]["score"],
        scores["stage_appropriateness"]["score"],
        scores["usefulness"]["score"],
    ]
    scores["overall_score"] = round(sum(dimension_scores) / len(dimension_scores), 2)

    return scores
