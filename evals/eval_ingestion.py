from __future__ import annotations

"""Document ingestion evaluation suite.

Checks that document-enriched extractions have more populated fields,
proper source attribution, and flagged discrepancies.

CLI:
    python -m evals.eval_ingestion --transcript data/transcripts/sample_lazo_call1.txt --pdf data/documents/English_LAZO_Pitch_Deck_design_ENERO..pdf
    python -m evals.eval_ingestion --transcript data/transcripts/sample_lazo_call1.txt --pdf data/documents/English_LAZO_Pitch_Deck_design_ENERO..pdf --skip-extraction
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.extraction.extractor import extract_from_transcript
from src.ingestion.document_processor import extract_from_document
from src.ingestion.merger import merge_extractions, _count_populated_fields

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: str


def run_programmatic_evals(
    transcript_extraction: dict,
    document_extraction: dict,
    merged_extraction: dict,
) -> list[EvalResult]:
    """Run deterministic checks on document ingestion quality.

    Args:
        transcript_extraction: Extraction from transcript only.
        document_extraction: Extraction from document only.
        merged_extraction: Merged extraction with source attribution.

    Returns:
        List of EvalResult with pass/fail for each check.
    """
    results = []

    # 1. Enrichment — merged has >= fields than transcript alone
    t_fields = _count_populated_fields(transcript_extraction)
    m_fields_raw = _count_merged_attributed_fields(merged_extraction)
    enriched = len(m_fields_raw) >= len(t_fields)
    results.append(EvalResult(
        name="enrichment_gain",
        passed=enriched,
        details=f"Transcript: {len(t_fields)} fields, Merged: {len(m_fields_raw)} attributed fields. "
                f"{'Enriched' if enriched else 'NOT enriched'}",
    ))

    # 2. Document contributes new fields
    d_fields = _count_populated_fields(document_extraction)
    doc_only = d_fields - t_fields
    has_new_fields = len(doc_only) > 0
    results.append(EvalResult(
        name="document_new_fields",
        passed=has_new_fields,
        details=f"Document contributed {len(doc_only)} new field(s): "
                f"{sorted(list(doc_only))[:5]}{'...' if len(doc_only) > 5 else ''}",
    ))

    # 3. Source attribution present — merged fields have "source" keys
    attributed_count = 0
    total_leaf_count = 0
    _count_attributions(merged_extraction, attributed_count=attributed_count, stats={"attributed": 0, "total": 0})
    stats = {"attributed": 0, "total": 0}
    _count_attributions(merged_extraction, stats=stats)
    has_attribution = stats["attributed"] > 0
    results.append(EvalResult(
        name="source_attribution_present",
        passed=has_attribution,
        details=f"{stats['attributed']} attributed fields out of {stats['total']} leaf fields",
    ))

    # 4. Combined sources list has both transcript and deck entries
    sources = merged_extraction.get("sources", [])
    source_types = {s.get("source_type") for s in sources if isinstance(s, dict)}
    has_both = "transcript" in source_types and "deck" in source_types
    results.append(EvalResult(
        name="combined_sources_list",
        passed=has_both,
        details=f"Source types: {sorted(source_types)}. {'Both present' if has_both else 'Missing type(s)'}",
    ))

    # 5. Enrichment stats present
    stats_present = "_enrichment_stats" in merged_extraction
    results.append(EvalResult(
        name="enrichment_stats_present",
        passed=stats_present,
        details=f"Stats: {merged_extraction.get('_enrichment_stats', 'missing')}",
    ))

    # 6. Discrepancies field present (even if empty)
    disc_present = "_discrepancies" in merged_extraction
    disc_count = len(merged_extraction.get("_discrepancies", []))
    results.append(EvalResult(
        name="discrepancies_tracked",
        passed=disc_present,
        details=f"Discrepancies field present: {disc_present}, count: {disc_count}",
    ))

    # 7. Document extraction has deck source type
    doc_sources = document_extraction.get("sources", [])
    deck_sources = [s for s in doc_sources if isinstance(s, dict) and s.get("source_type") == "deck"]
    has_deck_sources = len(deck_sources) > 0
    results.append(EvalResult(
        name="document_deck_source_type",
        passed=has_deck_sources,
        details=f"Document has {len(deck_sources)} deck-sourced entries out of {len(doc_sources)} total",
    ))

    return results


def _count_merged_attributed_fields(merged: dict, prefix: str = "") -> set[str]:
    """Count fields in merged extraction that have source attribution."""
    fields = set()
    for key, val in merged.items():
        if key.startswith("_") or key in ("sources", "call_stage"):
            continue
        field_path = f"{prefix}.{key}" if prefix else key

        if isinstance(val, dict) and "value" in val and "source" in val:
            # This is an attributed field
            if val.get("value") is not None:
                fields.add(field_path)
        elif isinstance(val, dict):
            fields.update(_count_merged_attributed_fields(val, field_path))
        elif isinstance(val, list) and val:
            # Check if list items are attributed
            for i, item in enumerate(val):
                if isinstance(item, dict) and "value" in item and "source" in item:
                    fields.add(f"{field_path}[{i}]")
    return fields


def _count_attributions(data, prefix: str = "", stats: dict | None = None, **kwargs):
    """Count how many leaf fields have source attribution."""
    if stats is None:
        stats = {"attributed": 0, "total": 0}

    if isinstance(data, dict):
        if "value" in data and "source" in data:
            # Attributed leaf
            stats["total"] += 1
            if data.get("source") is not None:
                stats["attributed"] += 1
            return

        for key, val in data.items():
            if key.startswith("_") or key in ("sources", "call_stage"):
                continue
            _count_attributions(val, f"{prefix}.{key}" if prefix else key, stats)

    elif isinstance(data, list):
        for i, item in enumerate(data):
            _count_attributions(item, f"{prefix}[{i}]", stats)


def run_ingestion_eval(
    transcript_path: str | Path,
    pdf_path: str | Path,
    call_stage: int = 1,
    *,
    skip_extraction: bool = False,
    output_dir: str | Path | None = None,
) -> list[EvalResult]:
    """Run the full ingestion eval: extract from both sources, merge, check.

    Args:
        transcript_path: Path to transcript file.
        pdf_path: Path to PDF document.
        call_stage: Call stage schema to use.
        skip_extraction: If True, look for pre-computed extractions.
        output_dir: Optional dir to write results.

    Returns:
        List of EvalResult.
    """
    transcript_path = Path(transcript_path)
    pdf_path = Path(pdf_path)

    client = anthropic.Anthropic()

    # Transcript extraction
    if skip_extraction:
        # Try to load pre-computed
        precomputed = _find_precomputed(transcript_path, call_stage)
        if precomputed:
            print(f"Using pre-computed transcript extraction", file=sys.stderr)
            transcript_extraction = precomputed
        else:
            print(f"No pre-computed extraction found, running live...", file=sys.stderr)
            transcript = transcript_path.read_text()
            transcript_extraction = extract_from_transcript(transcript, call_stage, client=client)
    else:
        print(f"Extracting from transcript: {transcript_path.name}...", file=sys.stderr)
        transcript = transcript_path.read_text()
        transcript_extraction = extract_from_transcript(transcript, call_stage, client=client)

    print(f"Transcript extraction: {len(_count_populated_fields(transcript_extraction))} populated fields", file=sys.stderr)

    # Document extraction
    print(f"Extracting from document: {pdf_path.name}...", file=sys.stderr)
    document_extraction = extract_from_document(pdf_path, call_stage, client=client)
    print(f"Document extraction: {len(_count_populated_fields(document_extraction))} populated fields", file=sys.stderr)

    # Merge
    print(f"Merging extractions...", file=sys.stderr)
    merged = merge_extractions(transcript_extraction, document_extraction)

    # Run evals
    results = run_programmatic_evals(transcript_extraction, document_extraction, merged)

    # Print results
    _print_results(results, merged)

    # Write outputs
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        (output_dir / "transcript_extraction.json").write_text(
            json.dumps(transcript_extraction, indent=2, ensure_ascii=False)
        )
        (output_dir / "document_extraction.json").write_text(
            json.dumps(document_extraction, indent=2, ensure_ascii=False)
        )
        (output_dir / "merged_extraction.json").write_text(
            json.dumps(merged, indent=2, ensure_ascii=False)
        )
        (output_dir / "ingestion_eval.json").write_text(
            json.dumps(
                [{"name": r.name, "passed": r.passed, "details": r.details} for r in results],
                indent=2,
            )
        )
        print(f"\nResults written to {output_dir}", file=sys.stderr)

    return results


def _find_precomputed(transcript_path: Path, call_stage: int) -> dict | None:
    """Look for a pre-computed extraction."""
    stem = transcript_path.stem
    parts = stem.split("_")
    for i, part in enumerate(parts):
        if part.lower().startswith("call") and len(part) > 4:
            call_num = part[4:]
            company = "_".join(parts[1:i])
            output_path = DATA_DIR / "output" / company / f"extraction_call{call_num}.json"
            if output_path.exists():
                return json.loads(output_path.read_text())
    return None


def _print_results(results: list[EvalResult], merged: dict):
    """Print eval results summary."""
    print(f"\n{'='*70}", file=sys.stderr)
    print("DOCUMENT INGESTION EVAL", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    passed = sum(1 for r in results if r.passed)
    total = len(results)

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.details}", file=sys.stderr)

    stats = merged.get("_enrichment_stats", {})
    if stats:
        print(f"\n  Enrichment stats:", file=sys.stderr)
        for k, v in stats.items():
            print(f"    {k}: {v}", file=sys.stderr)

    print(f"\n  Result: {passed}/{total} checks passed", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Run document ingestion evaluation."
    )
    parser.add_argument(
        "--transcript", required=True, help="Path to transcript .txt file"
    )
    parser.add_argument(
        "--pdf", required=True, help="Path to PDF document"
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        default=1,
        help="Call stage schema (default: 1).",
    )
    parser.add_argument(
        "--skip-extraction",
        action="store_true",
        help="Use pre-computed transcript extraction if available.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to write eval results.",
    )

    args = parser.parse_args()

    for path, label in [(args.transcript, "Transcript"), (args.pdf, "PDF")]:
        if not Path(path).exists():
            print(f"Error: {label} not found: {path}", file=sys.stderr)
            sys.exit(1)

    results = run_ingestion_eval(
        transcript_path=args.transcript,
        pdf_path=args.pdf,
        call_stage=args.call_stage,
        skip_extraction=args.skip_extraction,
        output_dir=args.output_dir,
    )

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
