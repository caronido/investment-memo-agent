"""LLM-as-judge for memo quality evaluation.

Uses Claude Haiku to score memos on completeness, factual accuracy,
analytical quality, and template compliance.
"""

import json

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator for an SPV network's investment memo pipeline. You will be \
given an extraction (the structured data from a founder call) and the investment memo \
generated from it.

Score the memo on four dimensions, each from 1 to 5:

1. **Completeness** (1-5): Does the memo cover all available data from the extraction? \
Are sections that should have content actually written? Are [TBD] markers used \
appropriately for missing data only?
   - 5: All available data incorporated, [TBD] only where data truly missing
   - 3: Most data used but some available information omitted
   - 1: Significant data from extraction not reflected in memo

2. **Factual Accuracy** (1-5): Does every claim in the memo accurately reflect the \
extraction data? Are numbers correct? Are there any hallucinations or embellishments \
beyond what the extraction states? THIS IS THE HIGHEST BAR — even one fabricated \
detail drops the score significantly.
   - 5: Every fact traceable to extraction, zero hallucinations
   - 3: Mostly accurate with minor embellishments or imprecise phrasing
   - 1: Contains fabricated information or materially incorrect claims

3. **Analytical Quality** (1-5): Is the analysis specific to THIS company, not generic? \
Does it connect data points into investment-relevant insights? Does it demonstrate \
critical thinking about the opportunity?
   - 5: Sharp, company-specific analysis with connected insights
   - 3: Mix of specific and generic analysis
   - 1: Generic template language that could apply to any company

4. **Template Compliance** (1-5): Does the memo follow the Nido Ventures template \
structure? Are all 13 sections present? Is the header formatted correctly? Does it \
read like a professional investment memo?
   - 5: Perfect template adherence, professional tone throughout
   - 3: Most sections present, generally professional but some formatting issues
   - 1: Missing sections, inconsistent formatting, unprofessional tone

Return ONLY a JSON object with this exact structure:
{
  "completeness": {"score": N, "reasoning": "..."},
  "factual_accuracy": {"score": N, "reasoning": "..."},
  "analytical_quality": {"score": N, "reasoning": "..."},
  "template_compliance": {"score": N, "reasoning": "..."}
}

No markdown fences, no extra text. Just the JSON object."""


def judge_memo(
    extraction: dict,
    memo: str,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Score a memo using Claude Haiku as judge.

    Args:
        extraction: The extraction dict the memo was based on.
        memo: The Markdown memo text to evaluate.
        client: Optional Anthropic client.

    Returns:
        Dict with completeness, factual_accuracy, analytical_quality,
        template_compliance scores and reasoning, plus overall_score.
    """
    if client is None:
        client = anthropic.Anthropic()

    user_message = (
        "## EXTRACTION\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}\n\n"
        "## MEMO\n\n"
        f"{memo}"
    )

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
                {"role": "user", "content": "Your response was not valid JSON. Please return ONLY a valid JSON object with the four scoring dimensions, no extra text."},
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
        scores["completeness"]["score"],
        scores["factual_accuracy"]["score"],
        scores["analytical_quality"]["score"],
        scores["template_compliance"]["score"],
    ]
    scores["overall_score"] = round(sum(dimension_scores) / len(dimension_scores), 2)

    return scores
