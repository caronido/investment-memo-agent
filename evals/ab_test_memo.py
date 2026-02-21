"""A/B test memo generation prompt variants.

Runs all prompt variants against available extractions, scores each with the
eval suite, and produces comparison tables.

CLI:
    python -m evals.ab_test_memo
    python -m evals.ab_test_memo --variants v1_analyst_template v3_skeptical_analyst
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import anthropic

from evals.eval_memo import run_programmatic_evals, run_judge_evals
from src.memo_generation.generator import MODEL, _build_section_guide, _load_memo_template
from src.memo_generation.prompt_variants import MEMO_VARIANT_DESCRIPTIONS, MEMO_VARIANTS

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _generate_with_variant(
    extraction: dict,
    gap_analysis: dict | None,
    variant_prompt: str,
    client: anthropic.Anthropic,
) -> str:
    """Generate a memo using a specific prompt variant."""
    memo_template = _load_memo_template()
    call_stage = extraction.get("call_stage", 1)
    section_guide = _build_section_guide(memo_template, call_stage)

    company_name = "Unknown"
    company = extraction.get("company", {})
    if isinstance(company, dict):
        company_name = company.get("name", "Unknown")

    parts = [
        f"## CURRENT EXTRACTION (Call {call_stage})\n\n"
        f"{json.dumps(extraction, indent=2, ensure_ascii=False)}",
    ]

    if gap_analysis:
        parts.append(
            f"\n\n## GAP ANALYSIS\n\n"
            f"{json.dumps(gap_analysis, indent=2, ensure_ascii=False)}"
        )

    parts.append(f"\n\n## MEMO TEMPLATE — SECTION GUIDE\n\n{section_guide}")
    parts.append(
        f"\n\n## METADATA\n"
        f"- Company: {company_name}\n"
        f"- Memo version: {call_stage}\n"
        f"- Date: {date.today().isoformat()}\n"
        f"- Call stage just completed: {call_stage}\n"
        "\nGenerate the full investment memo. Write sections with data fully. "
        "Use [TBD] placeholders for sections without data. Return only the Markdown memo."
    )

    user_message = "\n\n".join(parts)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=variant_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text


def _discover_test_data() -> list[dict]:
    """Find extraction + gap analysis pairs in data/output/."""
    output_dir = DATA_DIR / "output"
    results = []
    if not output_dir.exists():
        return results

    for company_dir in sorted(output_dir.iterdir()):
        if not company_dir.is_dir():
            continue
        for ext_file in sorted(company_dir.glob("extraction_call*.json")):
            call_num = ext_file.stem.replace("extraction_call", "")
            gap_file = company_dir / f"gap_analysis_call{call_num}.json"
            entry = {
                "company": company_dir.name,
                "call_stage": int(call_num),
                "extraction_path": ext_file,
            }
            if gap_file.exists():
                entry["gap_path"] = gap_file
            results.append(entry)

    return results


def run_ab_test(
    variant_names: list[str] | None = None,
) -> dict:
    """Run A/B test across memo variants.

    Returns:
        Dict with results and summary.
    """
    if variant_names is None:
        variant_names = list(MEMO_VARIANTS.keys())

    test_data = _discover_test_data()
    if not test_data:
        print("No extraction data found in data/output/")
        return {"results": [], "summary": []}

    client = anthropic.Anthropic()

    # Pre-load data
    data_entries = []
    for td in test_data:
        extraction = json.loads(td["extraction_path"].read_text())
        gap_analysis = None
        if "gap_path" in td:
            gap_analysis = json.loads(td["gap_path"].read_text())
        data_entries.append({
            "company": td["company"],
            "call_stage": td["call_stage"],
            "extraction": extraction,
            "gap_analysis": gap_analysis,
        })

    print(f"Test data: {len(data_entries)} extraction(s)")
    print(f"Variants: {len(variant_names)}")
    print(f"Total runs: {len(data_entries) * len(variant_names)}")
    print()

    all_results = []

    for variant_name in variant_names:
        variant_prompt = MEMO_VARIANTS[variant_name]
        desc = MEMO_VARIANT_DESCRIPTIONS[variant_name]
        print(f"\n{'='*70}")
        print(f"VARIANT: {variant_name}")
        print(f"  {desc}")
        print(f"{'='*70}")

        for de in data_entries:
            label = f"{de['company']}_call{de['call_stage']}"
            print(f"\n  {label}...")

            # Generate memo
            try:
                memo = _generate_with_variant(
                    de["extraction"], de["gap_analysis"],
                    variant_prompt, client,
                )
            except Exception as e:
                print(f"    GENERATION FAILED: {e}")
                all_results.append({
                    "variant": variant_name, "data": label, "error": str(e),
                })
                continue

            # Programmatic evals
            prog_results = run_programmatic_evals(
                memo, de["extraction"], de["gap_analysis"],
            )
            passed = sum(1 for r in prog_results if r.passed)
            total = len(prog_results)
            print(f"    Programmatic: {passed}/{total}")
            for r in prog_results:
                if not r.passed:
                    print(f"      [FAIL] {r.name}: {r.details}")

            # Judge evals
            judge_scores = run_judge_evals(de["extraction"], memo, client=client)
            print(
                f"    Judge: comp={judge_scores['completeness']['score']} "
                f"fact={judge_scores['factual_accuracy']['score']} "
                f"anal={judge_scores['analytical_quality']['score']} "
                f"tmpl={judge_scores['template_compliance']['score']} "
                f"overall={judge_scores['overall_score']}"
            )

            all_results.append({
                "variant": variant_name,
                "data": label,
                "prog_passed": passed,
                "prog_total": total,
                "completeness": judge_scores["completeness"]["score"],
                "factual_accuracy": judge_scores["factual_accuracy"]["score"],
                "analytical_quality": judge_scores["analytical_quality"]["score"],
                "template_compliance": judge_scores["template_compliance"]["score"],
                "overall": judge_scores["overall_score"],
            })

    # Compute summary
    summary = _compute_summary(all_results, variant_names)

    _print_per_data_table(all_results, variant_names)
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
            "avg_factual_accuracy": round(sum(r["factual_accuracy"] for r in vr) / n, 2),
            "avg_analytical_quality": round(sum(r["analytical_quality"] for r in vr) / n, 2),
            "avg_template_compliance": round(sum(r["template_compliance"] for r in vr) / n, 2),
            "avg_overall": round(sum(r["overall"] for r in vr) / n, 2),
        })
    return summary


def _print_per_data_table(results: list[dict], variant_names: list[str]):
    """Print per-data breakdown."""
    print(f"\n\n{'='*95}")
    print("PER-EXTRACTION BREAKDOWN")
    print(f"{'='*95}")

    data_labels = sorted(set(r["data"] for r in results if "error" not in r))
    for label in data_labels:
        print(f"\n  {label}:")
        header = f"    {'Variant':<25} {'Prog':>8} {'Comp':>5} {'Fact':>5} {'Anal':>5} {'Tmpl':>5} {'Overall':>7}"
        print(header)
        print(f"    {'-'*(len(header)-4)}")

        for vn in variant_names:
            matches = [r for r in results if r["variant"] == vn and r.get("data") == label]
            if not matches:
                continue
            r = matches[0]
            if "error" in r:
                print(f"    {vn:<25} {'ERROR':>8}")
                continue
            prog = f"{r['prog_passed']}/{r['prog_total']}"
            print(
                f"    {vn:<25} {prog:>8} {r['completeness']:>5} "
                f"{r['factual_accuracy']:>5} {r['analytical_quality']:>5} "
                f"{r['template_compliance']:>5} {r['overall']:>7}"
            )


def _print_summary_table(summary: list[dict]):
    """Print the summary comparison table."""
    print(f"\n\n{'='*95}")
    print("VARIANT COMPARISON (averages)")
    print(f"{'='*95}")

    header = f"{'Variant':<25} {'Runs':>4} {'Prog%':>6} {'Comp':>5} {'Fact':>5} {'Anal':>5} {'Tmpl':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for s in summary:
        if "error" in s:
            print(f"{s['variant']:<25} {s['runs']:>4} {'ERROR':>6}")
            continue
        prog_pct = f"{s['avg_prog_pass_rate']*100:.0f}%"
        print(
            f"{s['variant']:<25} {s['runs']:>4} {prog_pct:>6} "
            f"{s['avg_completeness']:>5} {s['avg_factual_accuracy']:>5} "
            f"{s['avg_analytical_quality']:>5} {s['avg_template_compliance']:>5} "
            f"{s['avg_overall']:>7}"
        )


def _print_winner(summary: list[dict]):
    """Identify and print the winning variant."""
    valid = [s for s in summary if "error" not in s]
    if not valid:
        print("\nNo valid results to determine a winner.")
        return

    # Sort by: factual accuracy first (highest bar), then overall
    valid.sort(
        key=lambda s: (s["avg_factual_accuracy"], s["avg_overall"]),
        reverse=True,
    )
    winner = valid[0]

    print(f"\n{'='*95}")
    print(f"WINNER: {winner['variant']}")
    print(f"  Overall: {winner['avg_overall']}/5")
    print(f"  Completeness: {winner['avg_completeness']}/5")
    print(f"  Factual Accuracy: {winner['avg_factual_accuracy']}/5")
    print(f"  Analytical Quality: {winner['avg_analytical_quality']}/5")
    print(f"  Template Compliance: {winner['avg_template_compliance']}/5")
    print(f"  Programmatic pass rate: {winner['avg_prog_pass_rate']*100:.0f}%")
    desc = MEMO_VARIANT_DESCRIPTIONS.get(winner["variant"], "")
    print(f"  Description: {desc}")
    print(f"{'='*95}")


def main():
    parser = argparse.ArgumentParser(
        description="A/B test memo generation prompt variants."
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=list(MEMO_VARIANTS.keys()),
        help="Specific variants to test (default: all)",
    )

    args = parser.parse_args()
    run_ab_test(variant_names=args.variants)


if __name__ == "__main__":
    main()
