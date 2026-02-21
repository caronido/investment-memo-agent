"""Memo generation: produces a Markdown investment memo from extraction + gap analysis.

Supports both initial generation (after first call) and updates (after subsequent calls)
via the existing_memo parameter.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.memo_generation.prompts import MEMO_SYSTEM_PROMPT, MEMO_UPDATE_PROMPT

load_dotenv()

MODEL = "claude-sonnet-4-20250514"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _load_memo_template() -> dict:
    """Load the memo template schema."""
    schema_path = SCHEMAS_DIR / "memo_template.json"
    return json.loads(schema_path.read_text())


def _build_section_guide(memo_template: dict, call_stage: int) -> str:
    """Build a section-by-section guide showing what to write and what's TBD."""
    sections = memo_template.get("section_definitions", [])
    lines = []
    for sec in sections:
        sid = sec["id"]
        title = sec["title"]
        desc = sec["description"]
        primary = sec.get("primary_call", 0)
        updated_by = sec.get("updated_by_calls", [])

        if call_stage in updated_by:
            status = "WRITE — data available from this call"
        elif primary > call_stage:
            status = f"TBD — primary data comes from Call {primary}"
        else:
            status = "TBD — no data yet"

        lines.append(f"- **{title}** ({sid}): {desc}\n  Status: {status}")

    return "\n".join(lines)


def generate_memo(
    extraction: dict,
    gap_analysis: dict | None = None,
    *,
    existing_memo: str | None = None,
    previous_extractions: list[dict] | None = None,
    client: anthropic.Anthropic | None = None,
) -> str:
    """Generate or update a Markdown investment memo.

    Args:
        extraction: Current call's extraction JSON.
        gap_analysis: Current call's gap analysis JSON (optional).
        existing_memo: Previous memo draft to update (for multi-call flow).
        previous_extractions: Extractions from prior calls (for context).
        client: Optional Anthropic client.

    Returns:
        Markdown string of the investment memo.
    """
    if client is None:
        client = anthropic.Anthropic()

    memo_template = _load_memo_template()
    call_stage = extraction.get("call_stage", 1)
    is_update = existing_memo is not None

    system_prompt = MEMO_UPDATE_PROMPT if is_update else MEMO_SYSTEM_PROMPT

    # Build the section guide
    section_guide = _build_section_guide(memo_template, call_stage)

    # Build user message
    parts = []

    if is_update:
        parts.append(
            f"## EXISTING MEMO (to update)\n\n{existing_memo}"
        )

    parts.append(
        f"## CURRENT EXTRACTION (Call {call_stage})\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}"
    )

    if previous_extractions:
        for prev in previous_extractions:
            prev_stage = prev.get("call_stage", "?")
            parts.append(
                f"\n\n## PREVIOUS EXTRACTION (Call {prev_stage})\n\n"
                f"{json.dumps(prev, indent=2, ensure_ascii=False)}"
            )

    if gap_analysis:
        parts.append(
            f"\n\n## GAP ANALYSIS\n\n"
            f"{json.dumps(gap_analysis, indent=2, ensure_ascii=False)}"
        )

    memo_version = call_stage
    company_name = "Unknown"
    company = extraction.get("company", {})
    if isinstance(company, dict):
        company_name = company.get("name", "Unknown")

    parts.append(
        f"\n\n## MEMO TEMPLATE — SECTION GUIDE\n\n{section_guide}"
    )

    parts.append(
        f"\n\n## METADATA\n"
        f"- Company: {company_name}\n"
        f"- Memo version: {memo_version}\n"
        f"- Date: {date.today().isoformat()}\n"
        f"- Call stage just completed: {call_stage}\n"
    )

    if is_update:
        parts.append(
            "\nUpdate the existing memo with the new data. Preserve accurate "
            "existing content. Fill in [TBD] placeholders where new data is available. "
            "Return the complete updated memo."
        )
    else:
        parts.append(
            "\nGenerate the full investment memo. Write sections with data fully. "
            "Use [TBD] placeholders for sections without data. Return only the Markdown memo."
        )

    user_message = "\n\n".join(parts)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text


def main():
    parser = argparse.ArgumentParser(
        description="Generate an investment memo from extraction and gap analysis."
    )
    parser.add_argument(
        "--extraction", required=True, help="Path to extraction JSON file"
    )
    parser.add_argument(
        "--gap-analysis", help="Path to gap analysis JSON file"
    )
    parser.add_argument(
        "--existing-memo", help="Path to existing memo .md file (for updates)"
    )
    parser.add_argument(
        "--previous-extractions",
        nargs="*",
        help="Paths to previous extraction JSON files",
    )
    parser.add_argument(
        "--output", required=True, help="Path to write memo .md file"
    )

    args = parser.parse_args()

    extraction_path = Path(args.extraction)
    if not extraction_path.exists():
        print(f"Error: Extraction not found: {extraction_path}", file=sys.stderr)
        sys.exit(1)

    extraction = json.loads(extraction_path.read_text())
    call_stage = extraction.get("call_stage", 1)
    company = extraction.get("company", {})
    company_name = company.get("name", "Unknown") if isinstance(company, dict) else "Unknown"
    print(f"Loaded extraction for {company_name} (call {call_stage})")

    gap_analysis = None
    if args.gap_analysis:
        gap_path = Path(args.gap_analysis)
        if not gap_path.exists():
            print(f"Error: Gap analysis not found: {gap_path}", file=sys.stderr)
            sys.exit(1)
        gap_analysis = json.loads(gap_path.read_text())
        print(f"Loaded gap analysis")

    existing_memo = None
    if args.existing_memo:
        memo_path = Path(args.existing_memo)
        if not memo_path.exists():
            print(f"Error: Existing memo not found: {memo_path}", file=sys.stderr)
            sys.exit(1)
        existing_memo = memo_path.read_text()
        print(f"Loaded existing memo ({len(existing_memo)} chars)")

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

    print(f"Generating memo...")

    memo = generate_memo(
        extraction,
        gap_analysis,
        existing_memo=existing_memo,
        previous_extractions=previous_extractions,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(memo)

    print(f"Memo saved to {output_path} ({len(memo)} chars)")

    # Print section summary
    section_count = memo.count("\n## ")
    tbd_count = memo.lower().count("[tbd")
    print(f"Sections: {section_count}, TBD placeholders: {tbd_count}")


if __name__ == "__main__":
    main()
