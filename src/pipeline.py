"""End-to-end pipeline: transcript → extraction → gap analysis → memo → eval report.

Orchestrates all three stages with a single shared Anthropic client,
optionally runs evals (programmatic + LLM judge) on each stage's output.

CLI:
    python -m src.pipeline --transcript data/transcripts/sample_lazo_call1.txt --call-stage 1 --output-dir data/output/lazo/
    python -m src.pipeline --transcript data/transcripts/sample_lazo_call1.txt --skip-evals
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from src.extraction.extractor import extract_from_transcript
from src.gap_analysis.analyzer import analyze_gaps
from src.memo_generation.generator import generate_memo

load_dotenv()


def _log(msg: str):
    """Print progress to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr)


def run_pipeline(
    transcript: str,
    call_stage: int | None = None,
    *,
    output_dir: str | Path | None = None,
    skip_evals: bool = False,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run the full memo-agent pipeline on a transcript.

    Args:
        transcript: Raw transcript text.
        call_stage: Call number (1-4). Auto-detected if None.
        output_dir: Directory to write output files. Prints to stdout if None.
        skip_evals: If True, skip the eval step (faster iteration).
        client: Optional shared Anthropic client.

    Returns:
        Dict with keys: extraction, gap_analysis, memo, eval_report (if not skipped).
    """
    if client is None:
        client = anthropic.Anthropic()

    result = {}

    # --- Stage 1: Extraction ---
    _log("Stage 1/3: Extracting structured data from transcript...")
    extraction = extract_from_transcript(transcript, call_stage, client=client)
    detected_stage = extraction.get("call_stage", call_stage or 1)
    result["extraction"] = extraction
    _log(f"  Extraction complete. Call stage: {detected_stage}, keys: {list(extraction.keys())}")

    # --- Stage 2: Gap Analysis ---
    _log("Stage 2/3: Running gap analysis...")
    gap_analysis = analyze_gaps(extraction, detected_stage, client=client)
    result["gap_analysis"] = gap_analysis
    questions = gap_analysis.get("follow_up_questions", [])
    doc_requests = gap_analysis.get("document_requests", [])
    _log(f"  Gap analysis complete. {len(questions)} questions, {len(doc_requests)} doc requests")

    # --- Stage 3: Memo Generation ---
    _log("Stage 3/3: Generating investment memo...")
    memo = generate_memo(extraction, gap_analysis, client=client)
    result["memo"] = memo
    section_count = memo.count("\n## ")
    tbd_count = memo.lower().count("[tbd")
    _log(f"  Memo complete. {section_count} sections, {tbd_count} TBD placeholders, {len(memo)} chars")

    # --- Stage 4: Evals (optional) ---
    if not skip_evals:
        _log("Running evals...")
        eval_report = _run_evals(transcript, extraction, gap_analysis, memo, detected_stage, client)
        result["eval_report"] = eval_report
    else:
        _log("Evals skipped (--skip-evals)")

    # --- Write output files ---
    if output_dir:
        _write_outputs(result, detected_stage, output_dir)

    return result


def _run_evals(
    transcript: str,
    extraction: dict,
    gap_analysis: dict,
    memo: str,
    call_stage: int,
    client: anthropic.Anthropic,
) -> dict:
    """Run programmatic + judge evals for all three stages."""
    from evals.eval_extraction import run_programmatic_evals as ext_prog, run_judge_evals as ext_judge
    from evals.eval_gap_analysis import run_programmatic_evals as gap_prog, run_judge_evals as gap_judge
    from evals.eval_memo import run_programmatic_evals as memo_prog, run_judge_evals as memo_judge

    report = {}

    # Extraction evals
    _log("  Evaluating extraction...")
    ext_prog_results = ext_prog(extraction, call_stage)
    ext_passed = sum(1 for r in ext_prog_results if r.passed)
    ext_total = len(ext_prog_results)

    ext_judge_scores = ext_judge(transcript, extraction, client=client)
    report["extraction"] = {
        "programmatic_passed": ext_passed,
        "programmatic_total": ext_total,
        "programmatic_details": [
            {"name": r.name, "passed": r.passed, "details": r.details}
            for r in ext_prog_results
        ],
        "judge_scores": ext_judge_scores,
    }
    _log(f"    Programmatic: {ext_passed}/{ext_total}, Judge: {ext_judge_scores.get('overall_score', '?')}/5")

    # Gap analysis evals
    _log("  Evaluating gap analysis...")
    gap_prog_results = gap_prog(gap_analysis, call_stage)
    gap_passed = sum(1 for r in gap_prog_results if r.passed)
    gap_total = len(gap_prog_results)

    gap_judge_scores = gap_judge(extraction, gap_analysis, call_stage, client=client)
    report["gap_analysis"] = {
        "programmatic_passed": gap_passed,
        "programmatic_total": gap_total,
        "programmatic_details": [
            {"name": r.name, "passed": r.passed, "details": r.details}
            for r in gap_prog_results
        ],
        "judge_scores": gap_judge_scores,
    }
    _log(f"    Programmatic: {gap_passed}/{gap_total}, Judge: {gap_judge_scores.get('overall_score', '?')}/5")

    # Memo evals
    _log("  Evaluating memo...")
    memo_prog_results = memo_prog(memo, extraction, gap_analysis)
    memo_passed = sum(1 for r in memo_prog_results if r.passed)
    memo_total = len(memo_prog_results)

    memo_judge_scores = memo_judge(extraction, memo, client=client)
    report["memo"] = {
        "programmatic_passed": memo_passed,
        "programmatic_total": memo_total,
        "programmatic_details": [
            {"name": r.name, "passed": r.passed, "details": r.details}
            for r in memo_prog_results
        ],
        "judge_scores": memo_judge_scores,
    }
    _log(f"    Programmatic: {memo_passed}/{memo_total}, Judge: {memo_judge_scores.get('overall_score', '?')}/5")

    # Summary
    total_prog_passed = ext_passed + gap_passed + memo_passed
    total_prog_total = ext_total + gap_total + memo_total
    avg_judge = (
        (ext_judge_scores.get("overall_score", 0)
         + gap_judge_scores.get("overall_score", 0)
         + memo_judge_scores.get("overall_score", 0))
        / 3
    )
    report["summary"] = {
        "total_programmatic": f"{total_prog_passed}/{total_prog_total}",
        "avg_judge_score": round(avg_judge, 2),
        "extraction_judge": ext_judge_scores.get("overall_score", 0),
        "gap_judge": gap_judge_scores.get("overall_score", 0),
        "memo_judge": memo_judge_scores.get("overall_score", 0),
    }
    _log(f"  Summary: {total_prog_passed}/{total_prog_total} programmatic, {avg_judge:.2f}/5 avg judge")

    return report


def _write_outputs(result: dict, call_stage: int, output_dir: str | Path):
    """Write pipeline outputs to files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Extraction
    ext_path = output_dir / f"extraction_call{call_stage}.json"
    ext_path.write_text(json.dumps(result["extraction"], indent=2, ensure_ascii=False))
    _log(f"  Wrote {ext_path}")

    # Gap analysis
    gap_path = output_dir / f"gap_analysis_call{call_stage}.json"
    gap_path.write_text(json.dumps(result["gap_analysis"], indent=2, ensure_ascii=False))
    _log(f"  Wrote {gap_path}")

    # Memo
    memo_path = output_dir / f"memo_v{call_stage}.md"
    memo_path.write_text(result["memo"])
    _log(f"  Wrote {memo_path}")

    # Eval report
    if "eval_report" in result:
        eval_path = output_dir / "eval_report.json"
        eval_path.write_text(json.dumps(result["eval_report"], indent=2, ensure_ascii=False))
        _log(f"  Wrote {eval_path}")


def _print_summary_table(result: dict):
    """Print a summary table to stderr."""
    report = result.get("eval_report")
    if not report:
        _log("\nNo eval report (evals were skipped)")
        return

    _log(f"\n{'='*70}")
    _log("PIPELINE RESULTS")
    _log(f"{'='*70}")

    header = f"{'Stage':<20} {'Programmatic':>15} {'Judge':>10}"
    _log(header)
    _log("-" * len(header))

    for stage_name, stage_key in [("Extraction", "extraction"), ("Gap Analysis", "gap_analysis"), ("Memo", "memo")]:
        stage = report[stage_key]
        prog = f"{stage['programmatic_passed']}/{stage['programmatic_total']}"
        judge = f"{stage['judge_scores'].get('overall_score', '?')}/5"
        _log(f"{stage_name:<20} {prog:>15} {judge:>10}")

    summary = report["summary"]
    _log("-" * len(header))
    _log(f"{'TOTAL':<20} {summary['total_programmatic']:>15} {summary['avg_judge_score']:.2f}/5")
    _log(f"{'='*70}")


def main():
    parser = argparse.ArgumentParser(
        description="Run the full memo-agent pipeline: transcript → extraction → gap analysis → memo → eval."
    )
    parser.add_argument(
        "--transcript", required=True, help="Path to transcript .txt file"
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        help="Call stage (1-4). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to write output files. If omitted, prints memo to stdout.",
    )
    parser.add_argument(
        "--skip-evals",
        action="store_true",
        help="Skip eval step for faster iteration.",
    )

    args = parser.parse_args()

    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"Error: Transcript file not found: {transcript_path}", file=sys.stderr)
        sys.exit(1)

    transcript = transcript_path.read_text()
    _log(f"Loaded transcript: {len(transcript)} chars from {transcript_path}")

    result = run_pipeline(
        transcript,
        call_stage=args.call_stage,
        output_dir=args.output_dir,
        skip_evals=args.skip_evals,
    )

    # Print summary table
    _print_summary_table(result)

    # If no output dir, print memo to stdout
    if not args.output_dir:
        print(result["memo"])


if __name__ == "__main__":
    main()
