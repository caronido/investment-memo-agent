"""LLM-as-judge for recommendation quality evaluation.

Uses Claude Haiku to score recommendations on evidence grounding,
calibration, and decision consistency.
"""

import json

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator for an SPV network's investment recommendation pipeline. \
You will be given the extractions from multiple founder calls, the investment memo, \
and the recommendation produced by the system.

Score the recommendation on three dimensions, each from 1 to 5:

1. **Evidence Grounding** (1-5): Are the rubric scores and rationales supported by \
specific evidence from the extractions and memo? Does every claim trace back to data?
   - 5: Every score justified with specific, traceable evidence from the data
   - 3: Most scores grounded but some rationales are generic or unsupported
   - 1: Scores feel arbitrary, rationales don't connect to extraction data

2. **Calibration** (1-5): Are the scores appropriately calibrated? A 4/5 should \
reflect genuinely strong evidence, not just founder claims. A 2/5 should reflect \
real weakness, not missing data. Scores should distinguish between "we don't know" \
and "this is weak."
   - 5: Scores precisely calibrated — strong evidence gets high scores, weak areas get low
   - 3: Generally reasonable but some scores feel too generous or too harsh
   - 1: Scores are systematically biased (all high or all low) regardless of evidence

3. **Decision Consistency** (1-5): Does the final recommendation (INVEST/PASS/REVISIT) \
logically follow from the rubric scores and rationale? Would a reasonable analyst \
reach the same conclusion from these scores?
   - 5: Recommendation is the obvious conclusion from the scores and rationale
   - 3: Recommendation is defensible but not the only reasonable interpretation
   - 1: Recommendation contradicts the scores or rationale

Return ONLY a JSON object with this exact structure:
{
  "evidence_grounding": {"score": N, "reasoning": "..."},
  "calibration": {"score": N, "reasoning": "..."},
  "decision_consistency": {"score": N, "reasoning": "..."}
}

No markdown fences, no extra text. Just the JSON object."""


def judge_recommendation(
    extractions: list[dict],
    memo: str,
    recommendation: dict,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Score a recommendation using Claude Haiku as judge.

    Args:
        extractions: List of extraction dicts from all calls.
        memo: The final memo markdown.
        recommendation: The recommendation dict to evaluate.
        client: Optional Anthropic client.

    Returns:
        Dict with evidence_grounding, calibration, decision_consistency
        scores and reasoning, plus overall_score.
    """
    if client is None:
        client = anthropic.Anthropic()

    parts = []
    for ext in extractions:
        stage = ext.get("call_stage", "?")
        parts.append(
            f"## EXTRACTION (Call {stage})\n\n"
            f"{json.dumps(ext, indent=2, ensure_ascii=False)}"
        )
    parts.append(f"## MEMO\n\n{memo}")
    parts.append(
        f"## RECOMMENDATION\n\n"
        f"{json.dumps(recommendation, indent=2, ensure_ascii=False)}"
    )

    user_message = "\n\n".join(parts)
    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=JUDGE_SYSTEM_PROMPT,
        messages=messages,
    )

    response_text = response.content[0].text.strip()

    # Strip markdown fences if present
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        response_text = "\n".join(lines[start:end])

    try:
        scores = json.loads(response_text)
    except json.JSONDecodeError:
        # Retry once with feedback
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=JUDGE_SYSTEM_PROMPT,
            messages=messages + [
                {"role": "assistant", "content": response_text},
                {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a valid JSON object with the three scoring dimensions, no extra text."},
            ],
        )
        response_text = retry_response.content[0].text.strip()
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            start = 1 if lines[0].startswith("```") else 0
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            response_text = "\n".join(lines[start:end])
        scores = json.loads(response_text)

    # Compute overall score
    dimension_scores = [
        scores["evidence_grounding"]["score"],
        scores["calibration"]["score"],
        scores["decision_consistency"]["score"],
    ]
    scores["overall_score"] = round(sum(dimension_scores) / len(dimension_scores), 2)

    return scores
