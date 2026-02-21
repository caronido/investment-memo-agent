"""A/B test extraction prompt variants.

Runs all prompt variants against all test transcripts, scores each with the
eval suite, and produces comparison tables.

CLI:
    python -m evals.ab_test_extraction
    python -m evals.ab_test_extraction --variants v1_straightforward v2_analyst_persona
    python -m evals.ab_test_extraction --transcripts data/transcripts/sample_lazo_call1.txt
"""

import argparse
import json
import sys
from pathlib import Path

import anthropic

from evals.eval_extraction import run_programmatic_evals, run_judge_evals
from src.extraction.extractor import (
    MODEL,
    SCHEMAS_DIR,
    SCHEMA_FILES,
    _parse_json_response,
    _validate_required_fields,
    detect_call_theme,
)
from src.extraction.prompt_variants import VARIANT_DESCRIPTIONS, VARIANTS

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _extract_with_variant(
    transcript: str,
    call_stage: int,
    variant_prompts: dict[int, str],
    client: anthropic.Anthropic,
) -> dict:
    """Run extraction using a specific prompt variant."""
    schema_path = SCHEMAS_DIR / SCHEMA_FILES[call_stage]
    schema_text = schema_path.read_text()
    system_prompt = variant_prompts[call_stage].replace("{schema}", schema_text)

    user_message = (
        "Extract structured data from the following founder call transcript. "
        "Return only the JSON object.\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )

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
                        "a valid JSON object matching the schema, with no "
                        "additional text or markdown fences."
                    ),
                },
            ],
        )
        data = _parse_json_response(retry_response.content[0].text)

    data["call_stage"] = call_stage
    return data


def run_ab_test(
    variant_names: list[str] | None = None,
    transcript_paths: list[Path] | None = None,
) -> dict:
    """Run A/B test across variants and transcripts.

    Returns:
        Dict with structure:
        {
            "results": [{variant, transcript, call_stage, prog_passed, prog_total,
                         completeness, accuracy, signal_to_noise, overall}],
            "summary": [{variant, avg_completeness, avg_accuracy, avg_stn, avg_overall}]
        }
    """
    if variant_names is None:
        variant_names = list(VARIANTS.keys())
    if transcript_paths is None:
        transcript_dir = DATA_DIR / "transcripts"
        transcript_paths = sorted(transcript_dir.glob("*.txt"))

    gt_dir = DATA_DIR / "ground_truth"
    client = anthropic.Anthropic()

    # Pre-load transcripts and detect call stages
    transcripts = {}
    call_stages = {}
    ground_truths = {}
    for tp in transcript_paths:
        text = tp.read_text()
        transcripts[tp.name] = text
        call_stages[tp.name] = detect_call_theme(text, client=client)

        gt_path = gt_dir / f"{tp.stem}_gt.json"
        if gt_path.exists():
            ground_truths[tp.name] = json.loads(gt_path.read_text())

    print(f"Transcripts: {len(transcripts)}")
    print(f"Variants: {len(variant_names)}")
    print(f"Total runs: {len(transcripts) * len(variant_names)}")
    for name, stage in call_stages.items():
        gt_status = "with GT" if name in ground_truths else "no GT"
        print(f"  {name}: call stage {stage} ({gt_status})")
    print()

    all_results = []

    for variant_name in variant_names:
        variant_prompts = VARIANTS[variant_name]
        desc = VARIANT_DESCRIPTIONS[variant_name]
        print(f"\n{'='*70}")
        print(f"VARIANT: {variant_name}")
        print(f"  {desc}")
        print(f"{'='*70}")

        for tp_name, transcript in transcripts.items():
            call_stage = call_stages[tp_name]
            gt = ground_truths.get(tp_name)

            print(f"\n  {tp_name} (call {call_stage})...")

            # Run extraction
            try:
                extraction = _extract_with_variant(
                    transcript, call_stage, variant_prompts, client
                )
            except Exception as e:
                print(f"    EXTRACTION FAILED: {e}")
                all_results.append({
                    "variant": variant_name,
                    "transcript": tp_name,
                    "call_stage": call_stage,
                    "error": str(e),
                })
                continue

            # Programmatic evals
            prog_results = run_programmatic_evals(extraction, call_stage, gt)
            passed = sum(1 for r in prog_results if r.passed)
            total = len(prog_results)
            print(f"    Programmatic: {passed}/{total}")
            for r in prog_results:
                if not r.passed:
                    print(f"      [FAIL] {r.name}: {r.details}")

            # Judge evals
            judge_scores = run_judge_evals(transcript, extraction, client=client)
            print(
                f"    Judge: comp={judge_scores['completeness']['score']} "
                f"acc={judge_scores['accuracy']['score']} "
                f"s/n={judge_scores['signal_to_noise']['score']} "
                f"overall={judge_scores['overall_score']}"
            )

            all_results.append({
                "variant": variant_name,
                "transcript": tp_name,
                "call_stage": call_stage,
                "prog_passed": passed,
                "prog_total": total,
                "completeness": judge_scores["completeness"]["score"],
                "accuracy": judge_scores["accuracy"]["score"],
                "signal_to_noise": judge_scores["signal_to_noise"]["score"],
                "overall": judge_scores["overall_score"],
            })

    # Compute summary
    summary = _compute_summary(all_results, variant_names)

    # Print tables
    _print_per_transcript_table(all_results, variant_names)
    _print_summary_table(summary)
    _print_winner(summary)

    return {"results": all_results, "summary": summary}


def _compute_summary(results: list[dict], variant_names: list[str]) -> list[dict]:
    """Compute average scores per variant."""
    summary = []
    for vn in variant_names:
        vr = [r for r in results if r["variant"] == vn and "error" not in r]
        if not vr:
            summary.append({"variant": vn, "runs": 0, "error": "all runs failed"})
            continue
        n = len(vr)
        summary.append({
            "variant": vn,
            "runs": n,
            "avg_prog_pass_rate": round(
                sum(r["prog_passed"] / r["prog_total"] for r in vr) / n, 3
            ),
            "avg_completeness": round(sum(r["completeness"] for r in vr) / n, 2),
            "avg_accuracy": round(sum(r["accuracy"] for r in vr) / n, 2),
            "avg_signal_to_noise": round(sum(r["signal_to_noise"] for r in vr) / n, 2),
            "avg_overall": round(sum(r["overall"] for r in vr) / n, 2),
        })
    return summary


def _print_per_transcript_table(results: list[dict], variant_names: list[str]):
    """Print per-transcript breakdown."""
    print(f"\n\n{'='*90}")
    print("PER-TRANSCRIPT BREAKDOWN")
    print(f"{'='*90}")

    # Group by transcript
    transcripts = sorted(set(r["transcript"] for r in results))
    for tp_name in transcripts:
        print(f"\n  {tp_name}:")
        header = f"    {'Variant':<25} {'Prog':>8} {'Comp':>5} {'Acc':>5} {'S/N':>5} {'Overall':>7}"
        print(header)
        print(f"    {'-'*(len(header)-4)}")

        for vn in variant_names:
            matches = [
                r for r in results
                if r["variant"] == vn and r["transcript"] == tp_name
            ]
            if not matches:
                continue
            r = matches[0]
            if "error" in r:
                print(f"    {vn:<25} {'ERROR':>8}")
                continue
            prog = f"{r['prog_passed']}/{r['prog_total']}"
            print(
                f"    {vn:<25} {prog:>8} {r['completeness']:>5} "
                f"{r['accuracy']:>5} {r['signal_to_noise']:>5} {r['overall']:>7}"
            )


def _print_summary_table(summary: list[dict]):
    """Print the summary comparison table."""
    print(f"\n\n{'='*90}")
    print("VARIANT COMPARISON (averages across all transcripts)")
    print(f"{'='*90}")

    header = f"{'Variant':<25} {'Runs':>4} {'Prog%':>6} {'Comp':>5} {'Acc':>5} {'S/N':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for s in summary:
        if "error" in s:
            print(f"{s['variant']:<25} {s['runs']:>4} {'ERROR':>6}")
            continue
        prog_pct = f"{s['avg_prog_pass_rate']*100:.0f}%"
        print(
            f"{s['variant']:<25} {s['runs']:>4} {prog_pct:>6} "
            f"{s['avg_completeness']:>5} {s['avg_accuracy']:>5} "
            f"{s['avg_signal_to_noise']:>5} {s['avg_overall']:>7}"
        )


def _print_winner(summary: list[dict]):
    """Identify and print the winning variant."""
    valid = [s for s in summary if "error" not in s]
    if not valid:
        print("\nNo valid results to determine a winner.")
        return

    # Sort by overall score, then by accuracy (tiebreaker)
    valid.sort(key=lambda s: (s["avg_overall"], s["avg_accuracy"]), reverse=True)
    winner = valid[0]

    print(f"\n{'='*90}")
    print(f"WINNER: {winner['variant']}")
    print(f"  Overall: {winner['avg_overall']}/5")
    print(f"  Completeness: {winner['avg_completeness']}/5")
    print(f"  Accuracy: {winner['avg_accuracy']}/5")
    print(f"  Signal-to-noise: {winner['avg_signal_to_noise']}/5")
    print(f"  Programmatic pass rate: {winner['avg_prog_pass_rate']*100:.0f}%")
    desc = VARIANT_DESCRIPTIONS.get(winner["variant"], "")
    print(f"  Description: {desc}")
    print(f"{'='*90}")


def main():
    parser = argparse.ArgumentParser(
        description="A/B test extraction prompt variants."
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=list(VARIANTS.keys()),
        help="Specific variants to test (default: all)",
    )
    parser.add_argument(
        "--transcripts",
        nargs="+",
        help="Specific transcript file paths (default: all in data/transcripts/)",
    )

    args = parser.parse_args()

    transcript_paths = None
    if args.transcripts:
        transcript_paths = [Path(t) for t in args.transcripts]
        for tp in transcript_paths:
            if not tp.exists():
                print(f"Error: Transcript not found: {tp}", file=sys.stderr)
                sys.exit(1)

    run_ab_test(
        variant_names=args.variants,
        transcript_paths=transcript_paths,
    )


if __name__ == "__main__":
    main()
