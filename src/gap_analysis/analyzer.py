"""Gap analysis: compares extraction against memo template to find missing data.

Takes extraction JSON + call stage, produces missing questions, document requests,
and section confidence scores.
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.gap_analysis.prompts import GAP_ANALYSIS_PROMPTS

load_dotenv()

MODEL = "claude-sonnet-4-20250514"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _load_gap_schema() -> str:
    """Load the gap analysis output schema."""
    schema_path = SCHEMAS_DIR / "gap_analysis.json"
    return schema_path.read_text()


def _load_memo_template() -> dict:
    """Load the memo template schema."""
    schema_path = SCHEMAS_DIR / "memo_template.json"
    return json.loads(schema_path.read_text())


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        cleaned = "\n".join(lines[start:end])
    return json.loads(cleaned)


def analyze_gaps(
    extraction: dict,
    call_stage: int,
    *,
    previous_extractions: list[dict] | None = None,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Analyze gaps in extracted data and generate follow-up questions.

    Args:
        extraction: Extraction JSON from the current call.
        call_stage: Which call was just completed (1, 2, 3, or 4).
        previous_extractions: Optional list of extractions from prior calls.
        client: Optional Anthropic client (created if not provided).

    Returns:
        Gap analysis dict matching schemas/gap_analysis.json:
        - call_stage, company_name
        - coverage_summary with section confidence
        - follow_up_questions (specific, actionable)
        - document_requests
        - data_quality_flags

    Raises:
        ValueError: If call_stage is invalid.
        json.JSONDecodeError: If Claude returns unparseable JSON.
    """
    if client is None:
        client = anthropic.Anthropic()

    if call_stage not in GAP_ANALYSIS_PROMPTS:
        raise ValueError(f"Invalid call_stage: {call_stage}. Must be 1, 2, 3, or 4.")

    system_prompt = GAP_ANALYSIS_PROMPTS[call_stage]
    gap_schema = _load_gap_schema()
    memo_template = _load_memo_template()

    # Build the user message with all available data
    sections_info = json.dumps(
        memo_template.get("section_definitions", []),
        indent=2,
        ensure_ascii=False,
    )

    user_parts = [
        f"## CURRENT EXTRACTION (Call {call_stage})\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}",
    ]

    if previous_extractions:
        for prev in previous_extractions:
            prev_stage = prev.get("call_stage", "?")
            user_parts.append(
                f"\n\n## PREVIOUS EXTRACTION (Call {prev_stage})\n\n"
                f"{json.dumps(prev, indent=2, ensure_ascii=False)}"
            )

    user_parts.append(
        f"\n\n## MEMO SECTION DEFINITIONS\n\n{sections_info}"
    )

    user_parts.append(
        f"\n\n## OUTPUT SCHEMA\n\n{gap_schema}\n\n"
        "Analyze the gaps and return a single JSON object matching the output schema."
    )

    user_message = "\n".join(user_parts)

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
        # Retry once
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
                        "a valid JSON object matching the output schema, with no "
                        "additional text or markdown fences."
                    ),
                },
            ],
        )
        data = _parse_json_response(retry_response.content[0].text)

    # Ensure call_stage is set
    data["call_stage"] = call_stage

    # Extract company name from extraction if not set
    if "company_name" not in data or not data["company_name"]:
        company = extraction.get("company", {})
        if isinstance(company, dict):
            data["company_name"] = company.get("name", "Unknown")
        else:
            data["company_name"] = "Unknown"

    return data


def main():
    parser = argparse.ArgumentParser(
        description="Analyze gaps in extraction data and generate follow-up questions."
    )
    parser.add_argument(
        "--extraction", required=True, help="Path to extraction JSON file"
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        help="Which call was just completed. Auto-detected from extraction if omitted.",
    )
    parser.add_argument(
        "--previous-extractions",
        nargs="*",
        help="Paths to previous extraction JSON files (for multi-call context)",
    )
    parser.add_argument(
        "--output", required=True, help="Path to write gap analysis JSON"
    )

    args = parser.parse_args()

    extraction_path = Path(args.extraction)
    if not extraction_path.exists():
        print(f"Error: Extraction file not found: {extraction_path}", file=sys.stderr)
        sys.exit(1)

    extraction = json.loads(extraction_path.read_text())
    print(f"Loaded extraction: {list(extraction.keys())}")

    # Auto-detect call stage from extraction if not provided
    call_stage = args.call_stage
    if call_stage is None:
        call_stage = extraction.get("call_stage")
        if call_stage is None:
            print("Error: --call-stage not provided and extraction has no call_stage field", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-detected call stage from extraction: {call_stage}")

    previous_extractions = None
    if args.previous_extractions:
        previous_extractions = []
        for prev_path_str in args.previous_extractions:
            prev_path = Path(prev_path_str)
            if not prev_path.exists():
                print(f"Error: Previous extraction not found: {prev_path}", file=sys.stderr)
                sys.exit(1)
            previous_extractions.append(json.loads(prev_path.read_text()))
        print(f"Loaded {len(previous_extractions)} previous extraction(s)")

    print(f"Running gap analysis for call stage {call_stage}...")

    result = analyze_gaps(
        extraction,
        call_stage,
        previous_extractions=previous_extractions,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    # Print summary
    print(f"\nGap analysis saved to {output_path}")
    print(f"Company: {result.get('company_name', 'Unknown')}")

    coverage = result.get("coverage_summary", {})
    sections = coverage.get("sections", [])
    if sections:
        print(f"\nSection coverage:")
        for sec in sections:
            name = sec.get("section_name", "?")
            cov = sec.get("coverage", "?")
            print(f"  {name}: {cov}")

    questions = result.get("follow_up_questions", [])
    print(f"\nFollow-up questions: {len(questions)}")
    for q in questions:
        priority = q.get("priority", "?")
        print(f"  [{priority}] {q['question']}")

    doc_requests = result.get("document_requests", [])
    print(f"\nDocument requests: {len(doc_requests)}")
    for d in doc_requests:
        priority = d.get("priority", "?")
        print(f"  [{priority}] {d['document']} — {d.get('reason', '')}")

    flags = result.get("data_quality_flags", [])
    if flags:
        print(f"\nData quality flags: {len(flags)}")
        for f in flags:
            print(f"  [{f.get('severity', '?')}] {f.get('field', '?')}: {f.get('issue', '')}")


if __name__ == "__main__":
    main()
