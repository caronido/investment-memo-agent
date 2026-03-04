"""Transcript extraction: calls Claude to extract structured JSON from a call transcript."""

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.extraction.prompts import SYSTEM_PROMPTS, THEME_DETECTION_PROMPT

load_dotenv()

MODEL = "claude-sonnet-4-20250514"
DETECTION_MODEL = "claude-haiku-4-5-20251001"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

SCHEMA_FILES = {
    1: "extraction_call1.json",
    2: "extraction_call2.json",
    3: "extraction_call3.json",
    4: "extraction_call4.json",
}

THEME_NAMES = {
    1: "Founder Story",
    2: "Product Deep Dive",
    3: "GTM Validation",
    4: "Other",
}


def _load_schema(call_stage: int) -> str:
    """Load the JSON schema for a given call stage."""
    schema_path = SCHEMAS_DIR / SCHEMA_FILES[call_stage]
    return schema_path.read_text()


def _build_system_prompt(call_stage: int) -> str:
    """Build the full system prompt with schema injected."""
    schema_text = _load_schema(call_stage)
    template = SYSTEM_PROMPTS[call_stage]
    return template.replace("{schema}", schema_text)


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag) and closing fence
        lines = cleaned.split("\n")
        # Find first and last fence lines
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        cleaned = "\n".join(lines[start:end])
    return json.loads(cleaned)


def _validate_required_fields(data: dict, call_stage: int) -> list[str]:
    """Check that top-level required fields from the schema are present."""
    schema = json.loads(_load_schema(call_stage))
    required = schema.get("required", [])
    missing = [field for field in required if field not in data]
    return missing


def detect_call_theme(
    transcript: str,
    client: anthropic.Anthropic | None = None,
) -> int:
    """Classify a transcript into a call theme (1-4) using a fast model.

    Sends the first ~6000 chars of the transcript to Claude Haiku for
    lightweight classification.

    Args:
        transcript: Raw transcript text.
        client: Optional Anthropic client (created if not provided).

    Returns:
        Call stage int (1, 2, 3, or 4).
    """
    if client is None:
        client = anthropic.Anthropic()

    # Use first ~6000 chars for better classification — 2000 was too short
    # and caused many transcripts to look like Call 1 (founder background).
    excerpt = transcript[:6000]
    prompt = THEME_DETECTION_PROMPT.format(transcript_excerpt=excerpt)

    response = client.messages.create(
        model=DETECTION_MODEL,
        max_tokens=8,
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()

    # Parse the digit from the response
    for char in answer:
        if char in "1234":
            return int(char)

    # Default to 4 (Other) if we can't parse the response
    return 4


def extract_from_transcript(
    transcript: str,
    call_stage: int | None = None,
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Extract structured data from a transcript using Claude.

    Args:
        transcript: Raw transcript text.
        call_stage: Call number (1-4). If None, auto-detects from transcript.
        client: Optional Anthropic client (created if not provided).

    Returns:
        Parsed JSON dict matching the extraction schema. Includes a
        ``call_stage`` key so downstream consumers know which schema was used.

    Raises:
        ValueError: If call_stage is invalid or required fields are missing.
        json.JSONDecodeError: If Claude returns unparseable JSON after retry.
    """
    if client is None:
        client = anthropic.Anthropic()

    if call_stage is None:
        call_stage = detect_call_theme(transcript, client=client)

    if call_stage not in SYSTEM_PROMPTS:
        raise ValueError(f"Invalid call_stage: {call_stage}. Must be 1, 2, 3, or 4.")

    system_prompt = _build_system_prompt(call_stage)
    user_message = (
        "Extract structured data from the following founder call transcript. "
        "Return only the JSON object.\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )

    # First attempt
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = response.content[0].text

    try:
        data = _parse_json_response(response_text)
    except json.JSONDecodeError:
        # Retry once asking Claude to fix its JSON
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": response_text},
                {
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. Please return ONLY "
                        "a valid JSON object matching the schema, with no "
                        "additional text or markdown fences."
                    ),
                },
            ],
        )
        data = _parse_json_response(retry_response.content[0].text)

    # Ensure call_stage is in the output
    data["call_stage"] = call_stage

    # Validate required fields
    missing = _validate_required_fields(data, call_stage)
    if missing:
        raise ValueError(
            f"Extraction missing required fields: {missing}. "
            f"Got keys: {list(data.keys())}"
        )

    return data


def main():
    parser = argparse.ArgumentParser(
        description="Extract structured data from a founder call transcript."
    )
    parser.add_argument(
        "--transcript", required=True, help="Path to transcript .txt file"
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        help="Call stage (1=Founder Story, 2=Product Deep Dive, 3=GTM Validation, 4=Other). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--output", required=True, help="Path to write output JSON"
    )

    args = parser.parse_args()

    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"Error: Transcript file not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    transcript = transcript_path.read_text()
    print(f"Loaded transcript: {len(transcript)} chars from {transcript_path}")

    if args.call_stage is None:
        print("Auto-detecting call theme...")
        detected = detect_call_theme(transcript)
        print(f"Detected theme: {detected} ({THEME_NAMES[detected]})")
        call_stage = detected
    else:
        call_stage = args.call_stage

    print(f"Extracting for call stage {call_stage} ({THEME_NAMES[call_stage]})...")

    result = extract_from_transcript(transcript, call_stage)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"Extraction saved to {output_path}")
    print(f"Top-level keys: {list(result.keys())}")


if __name__ == "__main__":
    main()
