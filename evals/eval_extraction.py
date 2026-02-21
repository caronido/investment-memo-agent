"""Extraction evaluation suite.

Three eval types:
1. Programmatic: schema compliance, type checks, non-empty checks, ground truth comparison
2. LLM judge: completeness, accuracy, signal-to-noise via Claude Haiku
3. Test runner: discovers transcripts, runs evals, prints summary table

CLI:
    python -m evals.eval_extraction --transcript path/to/transcript.txt --ground-truth path/to/gt.json
    python -m evals.eval_extraction --all
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic

from evals.judges.extraction_judge import judge_extraction
from src.extraction.extractor import extract_from_transcript

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
BASELINES_PATH = Path(__file__).resolve().parent / "baselines.json"

SCHEMA_FILES = {
    1: "extraction_call1.json",
    2: "extraction_call2.json",
    3: "extraction_call3.json",
    4: "extraction_call4.json",
}


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: str


def _load_schema(call_stage: int) -> dict:
    """Load and parse the JSON schema for a call stage."""
    schema_path = SCHEMAS_DIR / SCHEMA_FILES[call_stage]
    return json.loads(schema_path.read_text())


def _check_type(value, type_spec) -> bool:
    """Check if a value matches a JSON schema type spec."""
    if isinstance(type_spec, list):
        return any(_check_type(value, t) for t in type_spec)
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "array":
        return isinstance(value, list)
    if type_spec == "object":
        return isinstance(value, dict)
    if type_spec == "null":
        return value is None
    return True


def run_programmatic_evals(
    extraction: dict,
    call_stage: int,
    ground_truth: dict | None = None,
) -> list[EvalResult]:
    """Run deterministic programmatic checks on an extraction.

    Args:
        extraction: The extraction dict to evaluate.
        call_stage: Which call stage schema to validate against.
        ground_truth: Optional ground truth dict for comparison.

    Returns:
        List of EvalResult with pass/fail for each check.
    """
    results = []
    schema = _load_schema(call_stage)

    # 1. Required fields present
    required = schema.get("required", [])
    missing = [f for f in required if f not in extraction]
    results.append(EvalResult(
        name="required_fields",
        passed=len(missing) == 0,
        details=f"Missing: {missing}" if missing else "All required fields present",
    ))

    # 2. Type checks for top-level fields
    properties = schema.get("properties", {})
    type_errors = []
    for field, field_schema in properties.items():
        if field not in extraction:
            continue
        expected_type = field_schema.get("type")
        if expected_type and not _check_type(extraction[field], expected_type):
            type_errors.append(
                f"{field}: expected {expected_type}, got {type(extraction[field]).__name__}"
            )
    results.append(EvalResult(
        name="type_checks",
        passed=len(type_errors) == 0,
        details="; ".join(type_errors) if type_errors else "All types correct",
    ))

    # 3. Non-empty checks for required fields
    empty_fields = []
    for field in required:
        val = extraction.get(field)
        if val is None:
            empty_fields.append(field)
        elif isinstance(val, str) and val.strip() == "":
            empty_fields.append(field)
        elif isinstance(val, (list, dict)) and len(val) == 0:
            empty_fields.append(field)
    results.append(EvalResult(
        name="non_empty_required",
        passed=len(empty_fields) == 0,
        details=f"Empty: {empty_fields}" if empty_fields else "All required fields non-empty",
    ))

    # 4. Nested required fields (e.g. company.name for call 1)
    nested_errors = []
    for field, field_schema in properties.items():
        if field not in extraction or not isinstance(field_schema, dict):
            continue
        if field_schema.get("type") == "object":
            nested_required = field_schema.get("required", [])
            if isinstance(extraction[field], dict):
                for nf in nested_required:
                    if nf not in extraction[field]:
                        nested_errors.append(f"{field}.{nf}")
    results.append(EvalResult(
        name="nested_required_fields",
        passed=len(nested_errors) == 0,
        details=f"Missing: {nested_errors}" if nested_errors else "All nested required fields present",
    ))

    # 5. Ground truth comparison (if provided)
    if ground_truth:
        gt_results = _compare_ground_truth(extraction, ground_truth)
        results.extend(gt_results)

    return results


def _compare_ground_truth(extraction: dict, ground_truth: dict) -> list[EvalResult]:
    """Compare extraction against ground truth using flexible matching."""
    results = []

    # Company name — case-insensitive exact match
    if "company_name" in ground_truth:
        ext_name = ""
        if isinstance(extraction.get("company"), dict):
            ext_name = extraction["company"].get("name", "")
        passed = ext_name.lower().strip() == ground_truth["company_name"].lower().strip()
        results.append(EvalResult(
            name="gt_company_name",
            passed=passed,
            details=f"Expected '{ground_truth['company_name']}', got '{ext_name}'",
        ))

    # Founder names — check each GT name appears in extraction
    if "founders" in ground_truth:
        ext_founders = extraction.get("founders", [])
        ext_names = [f.get("name", "").lower() for f in ext_founders if isinstance(f, dict)]
        missing_founders = []
        for gt_name in ground_truth["founders"]:
            found = any(gt_name.lower() in en for en in ext_names)
            if not found:
                missing_founders.append(gt_name)
        results.append(EvalResult(
            name="gt_founders",
            passed=len(missing_founders) == 0,
            details=f"Missing: {missing_founders}" if missing_founders else f"All {len(ground_truth['founders'])} founders found",
        ))

    # Flexible signal checks — check if keywords appear anywhere in extraction text
    extraction_text = json.dumps(extraction, ensure_ascii=False).lower()

    signal_checks = {
        "revenue_model": "gt_revenue_model",
        "pricing_signals": "gt_pricing_signals",
        "valuation_signals": "gt_valuation_signals",
        "customer_names": "gt_customer_names",
    }

    for gt_key, eval_name in signal_checks.items():
        if gt_key not in ground_truth:
            continue
        signals = ground_truth[gt_key]
        if isinstance(signals, str):
            signals = [signals]
        missing = [s for s in signals if s.lower() not in extraction_text]
        results.append(EvalResult(
            name=eval_name,
            passed=len(missing) == 0,
            details=f"Missing signals: {missing}" if missing else f"All {len(signals)} signals found",
        ))

    return results


def run_judge_evals(
    transcript: str,
    extraction: dict,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run LLM-as-judge evaluation on an extraction.

    Returns:
        Dict with completeness, accuracy, signal_to_noise scores and overall_score.
    """
    return judge_extraction(transcript, extraction, client=client)


def _load_precomputed_extraction(transcript_path: Path) -> dict | None:
    """Look for a pre-computed extraction in data/output/."""
    # Convention: data/transcripts/sample_lazo_call1.txt
    #          -> data/output/lazo/extraction_call1.json
    stem = transcript_path.stem  # e.g. sample_lazo_call1
    parts = stem.split("_")

    # Try to find company name and call stage from filename
    # Expected pattern: sample_{company}_call{N}
    for i, part in enumerate(parts):
        if part.startswith("call") and len(part) > 4:
            call_num = part[4:]
            company = "_".join(parts[1:i])  # everything between sample_ and _callN
            output_path = DATA_DIR / "output" / company / f"extraction_call{call_num}.json"
            if output_path.exists():
                return json.loads(output_path.read_text())

    return None


def run_all_evals(
    transcript_dir: Path | None = None,
    ground_truth_dir: Path | None = None,
    output_dir: Path | None = None,
) -> list[dict]:
    """Discover all transcripts and run the full eval suite.

    Args:
        transcript_dir: Directory with .txt transcript files. Defaults to data/transcripts/.
        ground_truth_dir: Directory with GT JSON files. Defaults to data/ground_truth/.
        output_dir: Not used currently, reserved for future output storage.

    Returns:
        List of summary dicts, one per transcript.
    """
    if transcript_dir is None:
        transcript_dir = DATA_DIR / "transcripts"
    if ground_truth_dir is None:
        ground_truth_dir = DATA_DIR / "ground_truth"

    transcripts = sorted(transcript_dir.glob("*.txt"))
    if not transcripts:
        print(f"No transcripts found in {transcript_dir}")
        return []

    client = anthropic.Anthropic()
    summaries = []

    for transcript_path in transcripts:
        print(f"\n{'='*60}")
        print(f"Evaluating: {transcript_path.name}")
        print(f"{'='*60}")

        transcript = transcript_path.read_text()

        # Check for pre-computed extraction
        extraction = _load_precomputed_extraction(transcript_path)
        if extraction:
            print(f"  Using pre-computed extraction")
            call_stage = extraction.get("call_stage", 1)
        else:
            print(f"  Running extraction...")
            extraction = extract_from_transcript(transcript, client=client)
            call_stage = extraction.get("call_stage", 1)

        print(f"  Call stage: {call_stage}")

        # Look for matching ground truth
        gt_path = ground_truth_dir / f"{transcript_path.stem}_gt.json"
        ground_truth = None
        if gt_path.exists():
            ground_truth = json.loads(gt_path.read_text())
            print(f"  Ground truth: {gt_path.name}")
        else:
            print(f"  No ground truth file found")

        # Run programmatic evals
        prog_results = run_programmatic_evals(extraction, call_stage, ground_truth)
        passed = sum(1 for r in prog_results if r.passed)
        total = len(prog_results)
        print(f"\n  Programmatic: {passed}/{total} passed")
        for r in prog_results:
            status = "PASS" if r.passed else "FAIL"
            print(f"    [{status}] {r.name}: {r.details}")

        # Run judge evals
        print(f"\n  Running LLM judge...")
        judge_scores = run_judge_evals(transcript, extraction, client=client)
        print(f"  Judge scores:")
        for dim in ["completeness", "accuracy", "signal_to_noise"]:
            s = judge_scores[dim]
            print(f"    {dim}: {s['score']}/5 — {s['reasoning']}")
        print(f"  Overall: {judge_scores['overall_score']}/5")

        summaries.append({
            "transcript": transcript_path.name,
            "call_stage": call_stage,
            "programmatic_passed": passed,
            "programmatic_total": total,
            "completeness": judge_scores["completeness"]["score"],
            "accuracy": judge_scores["accuracy"]["score"],
            "signal_to_noise": judge_scores["signal_to_noise"]["score"],
            "overall_score": judge_scores["overall_score"],
        })

    # Print summary table
    _print_summary_table(summaries)

    # Write baselines
    _update_baselines(summaries)

    return summaries


def _print_summary_table(summaries: list[dict]):
    """Print a formatted summary table."""
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    header = f"{'Transcript':<35} {'Call':>4} {'Prog':>8} {'Comp':>5} {'Acc':>5} {'S/N':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for s in summaries:
        prog = f"{s['programmatic_passed']}/{s['programmatic_total']}"
        print(
            f"{s['transcript']:<35} {s['call_stage']:>4} {prog:>8} "
            f"{s['completeness']:>5} {s['accuracy']:>5} {s['signal_to_noise']:>5} "
            f"{s['overall_score']:>7}"
        )


def _update_baselines(summaries: list[dict]):
    """Write or update baselines.json with best scores."""
    baselines = {}
    if BASELINES_PATH.exists():
        baselines = json.loads(BASELINES_PATH.read_text())

    for s in summaries:
        key = s["transcript"]
        existing = baselines.get(key, {})
        baselines[key] = {
            "call_stage": s["call_stage"],
            "programmatic_passed": max(
                s["programmatic_passed"],
                existing.get("programmatic_passed", 0),
            ),
            "programmatic_total": s["programmatic_total"],
            "completeness": max(s["completeness"], existing.get("completeness", 0)),
            "accuracy": max(s["accuracy"], existing.get("accuracy", 0)),
            "signal_to_noise": max(s["signal_to_noise"], existing.get("signal_to_noise", 0)),
            "overall_score": max(s["overall_score"], existing.get("overall_score", 0)),
        }

    BASELINES_PATH.write_text(json.dumps(baselines, indent=2) + "\n")
    print(f"\nBaselines written to {BASELINES_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Run extraction evaluation suite."
    )
    parser.add_argument(
        "--transcript",
        help="Path to a single transcript .txt file",
    )
    parser.add_argument(
        "--ground-truth",
        help="Path to ground truth .json file",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run evals on all transcripts in data/transcripts/",
    )

    args = parser.parse_args()

    if not args.all and not args.transcript:
        parser.error("Provide --transcript or --all")

    if args.all:
        run_all_evals()
        return

    # Single transcript mode
    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"Error: Transcript not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    transcript = transcript_path.read_text()

    # Load or run extraction
    extraction = _load_precomputed_extraction(transcript_path)
    client = anthropic.Anthropic()

    if extraction:
        print(f"Using pre-computed extraction")
        call_stage = extraction.get("call_stage", 1)
    else:
        print(f"Running extraction...")
        extraction = extract_from_transcript(transcript, client=client)
        call_stage = extraction.get("call_stage", 1)

    print(f"Call stage: {call_stage}")

    # Load ground truth if provided
    ground_truth = None
    if args.ground_truth:
        gt_path = Path(args.ground_truth)
        if not gt_path.exists():
            print(f"Error: Ground truth not found: {gt_path}", file=sys.stderr)
            sys.exit(1)
        ground_truth = json.loads(gt_path.read_text())

    # Run programmatic evals
    prog_results = run_programmatic_evals(extraction, call_stage, ground_truth)
    passed = sum(1 for r in prog_results if r.passed)
    total = len(prog_results)
    print(f"\nProgrammatic: {passed}/{total} passed")
    for r in prog_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.details}")

    # Run judge evals
    print(f"\nRunning LLM judge...")
    judge_scores = run_judge_evals(transcript, extraction, client=client)
    print(f"Judge scores:")
    for dim in ["completeness", "accuracy", "signal_to_noise"]:
        s = judge_scores[dim]
        print(f"  {dim}: {s['score']}/5 — {s['reasoning']}")
    print(f"Overall: {judge_scores['overall_score']}/5")


if __name__ == "__main__":
    main()
