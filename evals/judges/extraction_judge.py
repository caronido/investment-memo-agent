"""LLM-as-judge for extraction quality evaluation.

Uses Claude Haiku to score extractions on completeness, accuracy, and
signal-to-noise ratio.
"""

import json

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator for an SPV network's investment memo pipeline. You will be given \
a raw call transcript and a structured JSON extraction produced from that transcript.

Score the extraction on three dimensions, each from 1 to 5:

1. **Completeness** (1-5): How well does the extraction capture all relevant \
information from the transcript? Are key data points missing?
   - 5: All meaningful information captured
   - 3: Most key points captured, some minor omissions
   - 1: Major information gaps

2. **Accuracy** (1-5): Does the extracted data faithfully represent what was said? \
Are there hallucinations or misinterpretations?
   - 5: All data points accurately reflect the transcript
   - 3: Mostly accurate with minor errors
   - 1: Significant factual errors or hallucinations

3. **Signal-to-Noise** (1-5): Is the extraction focused on investment-relevant \
information? Does it avoid filler and irrelevant details?
   - 5: Clean, focused on decision-relevant data
   - 3: Some noise but mostly relevant
   - 1: Cluttered with irrelevant information

Return ONLY a JSON object with this exact structure:
{
  "completeness": {"score": N, "reasoning": "..."},
  "accuracy": {"score": N, "reasoning": "..."},
  "signal_to_noise": {"score": N, "reasoning": "..."}
}

No markdown fences, no extra text. Just the JSON object."""


def judge_extraction(
    transcript: str,
    extraction: dict,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Score an extraction using Claude Haiku as judge.

    Args:
        transcript: Raw transcript text.
        extraction: The structured extraction dict to evaluate.
        client: Optional Anthropic client (created if not provided).

    Returns:
        Dict with completeness, accuracy, signal_to_noise scores and reasoning,
        plus an overall_score (average of the three).
    """
    if client is None:
        client = anthropic.Anthropic()

    # Truncate transcript if very long to stay within context limits
    max_transcript_chars = 80_000
    truncated = transcript[:max_transcript_chars]
    if len(transcript) > max_transcript_chars:
        truncated += "\n\n[TRANSCRIPT TRUNCATED]"

    user_message = (
        "## TRANSCRIPT\n\n"
        f"{truncated}\n\n"
        "## EXTRACTION\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}"
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

    # Compute overall score as average
    dimension_scores = [
        scores["completeness"]["score"],
        scores["accuracy"]["score"],
        scores["signal_to_noise"]["score"],
    ]
    scores["overall_score"] = round(sum(dimension_scores) / len(dimension_scores), 2)

    return scores
