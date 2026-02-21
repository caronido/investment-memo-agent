"""End-to-end pipeline evaluation runner.

Discovers transcripts, runs the full pipeline on each, and collects combined
scores across all stages.

CLI:
    python -m evals.eval_pipeline
    python -m evals.eval_pipeline --transcript data/transcripts/sample_lazo_call1.txt
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.pipeline import run_pipeline

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def run_pipeline_evals(
    transcript_path: str | Path | None = None,
    transcript_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> list[dict]:
    """Run the full pipeline on one or more transcripts and collect eval scores.

    Args:
        transcript_path: Path to a single transcript (overrides transcript_dir).
        transcript_dir: Directory to discover .txt transcripts from.
            Defaults to data/transcripts/.
        output_dir: Optional directory to write combined results.

    Returns:
        List of summary dicts, one per transcript.
    """
    if transcript_path:
        transcripts = [Path(transcript_path)]
    else:
        if transcript_dir is None:
            transcript_dir = DATA_DIR / "transcripts"
        transcript_dir = Path(transcript_dir)
        transcripts = sorted(transcript_dir.glob("*.txt"))

    if not transcripts:
        print("No transcripts found", file=sys.stderr)
        return []

    summaries = []

    for t_path in transcripts:
        if not t_path.exists():
            print(f"Error: Transcript not found: {t_path}", file=sys.stderr)
            continue

        print(f"\n{'='*70}", file=sys.stderr)
        print(f"Pipeline eval: {t_path.name}", file=sys.stderr)
        print(f"{'='*70}", file=sys.stderr)

        transcript = t_path.read_text()

        result = run_pipeline(transcript, skip_evals=False)

        report = result.get("eval_report", {})
        call_stage = result["extraction"].get("call_stage", "?")

        summary = {
            "transcript": t_path.name,
            "call_stage": call_stage,
        }

        # Extraction scores
        ext = report.get("extraction", {})
        summary["ext_prog"] = f"{ext.get('programmatic_passed', 0)}/{ext.get('programmatic_total', 0)}"
        summary["ext_judge"] = ext.get("judge_scores", {}).get("overall_score", 0)

        # Gap analysis scores
        gap = report.get("gap_analysis", {})
        summary["gap_prog"] = f"{gap.get('programmatic_passed', 0)}/{gap.get('programmatic_total', 0)}"
        summary["gap_judge"] = gap.get("judge_scores", {}).get("overall_score", 0)

        # Memo scores
        memo = report.get("memo", {})
        summary["memo_prog"] = f"{memo.get('programmatic_passed', 0)}/{memo.get('programmatic_total', 0)}"
        summary["memo_judge"] = memo.get("judge_scores", {}).get("overall_score", 0)

        # Overall
        overall = report.get("summary", {})
        summary["total_prog"] = overall.get("total_programmatic", "0/0")
        summary["avg_judge"] = overall.get("avg_judge_score", 0)

        summaries.append(summary)

    _print_combined_table(summaries)

    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / "pipeline_eval_results.json"
        out_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=False))
        print(f"\nResults written to {out_path}", file=sys.stderr)

    return summaries


def _print_combined_table(summaries: list[dict]):
    """Print a combined summary table across all transcripts."""
    print(f"\n{'='*100}", file=sys.stderr)
    print("PIPELINE EVAL SUMMARY", file=sys.stderr)
    print(f"{'='*100}", file=sys.stderr)

    header = (
        f"{'Transcript':<30} {'Call':>4} "
        f"{'Ext Prog':>9} {'Ext J':>6} "
        f"{'Gap Prog':>9} {'Gap J':>6} "
        f"{'Memo Prog':>10} {'Memo J':>7} "
        f"{'Avg J':>6}"
    )
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    for s in summaries:
        print(
            f"{s['transcript']:<30} {s['call_stage']:>4} "
            f"{s['ext_prog']:>9} {s['ext_judge']:>6} "
            f"{s['gap_prog']:>9} {s['gap_judge']:>6} "
            f"{s['memo_prog']:>10} {s['memo_judge']:>7} "
            f"{s['avg_judge']:>6}",
            file=sys.stderr,
        )

    print(f"{'='*100}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Run end-to-end pipeline evaluation."
    )
    parser.add_argument(
        "--transcript",
        help="Path to a single transcript .txt file. If omitted, discovers all in data/transcripts/.",
    )
    parser.add_argument(
        "--output-dir",
        help="Directory to write eval results JSON.",
    )

    args = parser.parse_args()

    run_pipeline_evals(
        transcript_path=args.transcript,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
