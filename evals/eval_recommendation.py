from __future__ import annotations

"""Recommendation evaluation suite.

Programmatic evals: valid recommendation value, rubric completeness, score ranges,
rationale quality, confidence bounds, overall score consistency.
LLM judge: evidence grounding, calibration, decision consistency.
Backtest: Lazo 3-call data should produce PASS recommendation.

CLI:
    python -m evals.eval_recommendation --company lazo
    python -m evals.eval_recommendation --recommendation data/output/lazo/recommendation.json --extractions data/output/lazo/extraction_call1.json data/output/lazo/extraction_call2.json data/output/lazo/extraction_call3.json --memo data/output/lazo/memo_v3.md
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from evals.judges.recommendation_judge import judge_recommendation
from src.recommendation.engine import RUBRIC_DIMENSIONS, generate_recommendation

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
BASELINES_PATH = Path(__file__).resolve().parent / "baselines.json"

VALID_RECOMMENDATIONS = {"INVEST", "PASS", "REVISIT"}


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: str


def run_programmatic_evals(recommendation: dict) -> list[EvalResult]:
    """Run deterministic programmatic checks on a recommendation.

    Args:
        recommendation: The recommendation dict to evaluate.

    Returns:
        List of EvalResult with pass/fail for each check.
    """
    results = []
    rubric = recommendation.get("rubric", {})

    # 1. Recommendation is INVEST/PASS/REVISIT
    rec_value = recommendation.get("recommendation", "")
    results.append(EvalResult(
        name="valid_recommendation",
        passed=rec_value in VALID_RECOMMENDATIONS,
        details=f"Got '{rec_value}'" if rec_value in VALID_RECOMMENDATIONS else f"Invalid: '{rec_value}', expected one of {VALID_RECOMMENDATIONS}",
    ))

    # 2. All 6 rubric dimensions present with scores
    missing_dims = [d for d in RUBRIC_DIMENSIONS if d not in rubric]
    results.append(EvalResult(
        name="all_dimensions_present",
        passed=len(missing_dims) == 0,
        details=f"All {len(RUBRIC_DIMENSIONS)} dimensions present" if not missing_dims else f"Missing: {missing_dims}",
    ))

    # 3. All scores 1-5
    invalid_scores = []
    for dim in RUBRIC_DIMENSIONS:
        if dim in rubric:
            score = rubric[dim].get("score")
            if not isinstance(score, (int, float)) or score < 1 or score > 5:
                invalid_scores.append(f"{dim}={score}")
    results.append(EvalResult(
        name="scores_in_range",
        passed=len(invalid_scores) == 0,
        details="All scores 1-5" if not invalid_scores else f"Out of range: {invalid_scores}",
    ))

    # 4. All rationales non-empty (>20 chars)
    short_rationales = []
    for dim in RUBRIC_DIMENSIONS:
        if dim in rubric:
            rationale = rubric[dim].get("rationale", "")
            if len(rationale) < 20:
                short_rationales.append(f"{dim} ({len(rationale)} chars)")
    results.append(EvalResult(
        name="rationales_substantive",
        passed=len(short_rationales) == 0,
        details="All rationales >20 chars" if not short_rationales else f"Too short: {short_rationales}",
    ))

    # 5. Confidence score 0-100
    confidence = recommendation.get("confidence_score")
    valid_confidence = isinstance(confidence, (int, float)) and 0 <= confidence <= 100
    results.append(EvalResult(
        name="confidence_in_range",
        passed=valid_confidence,
        details=f"Confidence: {confidence}%" if valid_confidence else f"Invalid confidence: {confidence}",
    ))

    # 6. Overall rationale >100 chars
    overall_rationale = recommendation.get("overall_rationale", "")
    results.append(EvalResult(
        name="overall_rationale_substantive",
        passed=len(overall_rationale) > 100,
        details=f"Overall rationale: {len(overall_rationale)} chars" if len(overall_rationale) > 100 else f"Too short: {len(overall_rationale)} chars (need >100)",
    ))

    # 7. Overall score matches avg of 6 dimension scores (+-0.1)
    overall_score = recommendation.get("overall_score", 0)
    scores = [rubric[dim]["score"] for dim in RUBRIC_DIMENSIONS if dim in rubric]
    expected_avg = round(sum(scores) / len(scores), 2) if scores else 0
    score_match = abs(overall_score - expected_avg) <= 0.1
    results.append(EvalResult(
        name="overall_score_consistent",
        passed=score_match,
        details=f"Overall {overall_score} matches avg {expected_avg}" if score_match else f"Overall {overall_score} != avg {expected_avg} (delta {abs(overall_score - expected_avg):.2f})",
    ))

    return results


def run_judge_evals(
    extractions: list[dict],
    memo: str,
    recommendation: dict,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run LLM-as-judge evaluation on a recommendation."""
    return judge_recommendation(extractions, memo, recommendation, client=client)


def run_backtest(
    company: str = "lazo",
    expected_recommendation: str = "PASS",
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run backtest: generate recommendation from existing data and verify it matches expected.

    Args:
        company: Company name (must have data in data/output/{company}/).
        expected_recommendation: Expected recommendation value.
        client: Optional Anthropic client.

    Returns:
        Dict with recommendation, passed (bool), and details.
    """
    if client is None:
        client = anthropic.Anthropic()

    company_dir = DATA_DIR / "output" / company

    # Load all extractions
    extractions = []
    for call in [1, 2, 3]:
        ext_path = company_dir / f"extraction_call{call}.json"
        if ext_path.exists():
            extractions.append(json.loads(ext_path.read_text()))

    if not extractions:
        return {"passed": False, "details": f"No extractions found in {company_dir}"}

    # Load latest memo
    memo_path = None
    for v in [3, 2, 1]:
        candidate = company_dir / f"memo_v{v}.md"
        if candidate.exists():
            memo_path = candidate
            break

    if not memo_path:
        return {"passed": False, "details": f"No memo found in {company_dir}"}

    memo = memo_path.read_text()

    # Load gap analyses
    gap_analyses = []
    for call in [1, 2, 3]:
        gap_path = company_dir / f"gap_analysis_call{call}.json"
        if gap_path.exists():
            gap_analyses.append(json.loads(gap_path.read_text()))

    # Generate recommendation
    recommendation = generate_recommendation(
        extractions, memo, gap_analyses or None, client=client,
    )

    actual = recommendation.get("recommendation", "")
    passed = actual == expected_recommendation

    return {
        "recommendation": recommendation,
        "passed": passed,
        "details": f"Expected {expected_recommendation}, got {actual}" + (
            "" if passed else " — MISMATCH"
        ),
    }


def _print_results(
    prog_results: list[EvalResult],
    judge_scores: dict | None = None,
    backtest_result: dict | None = None,
):
    """Print formatted eval results."""
    passed = sum(1 for r in prog_results if r.passed)
    total = len(prog_results)
    print(f"\nProgrammatic: {passed}/{total} passed")
    for r in prog_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.details}")

    if judge_scores:
        print(f"\nJudge scores:")
        for dim in ["evidence_grounding", "calibration", "decision_consistency"]:
            s = judge_scores[dim]
            print(f"  {dim}: {s['score']}/5 — {s['reasoning']}")
        print(f"Overall: {judge_scores['overall_score']}/5")

    if backtest_result:
        status = "PASS" if backtest_result["passed"] else "FAIL"
        print(f"\nBacktest: [{status}] {backtest_result['details']}")


def main():
    parser = argparse.ArgumentParser(
        description="Run recommendation evaluation suite."
    )
    parser.add_argument("--company", help="Company name for backtest (e.g., lazo)")
    parser.add_argument("--recommendation", help="Path to recommendation JSON file")
    parser.add_argument("--extractions", nargs="+", help="Paths to extraction JSON files")
    parser.add_argument("--memo", help="Path to memo .md file")
    parser.add_argument("--skip-judge", action="store_true", help="Skip LLM judge eval")
    parser.add_argument("--skip-backtest", action="store_true", help="Skip backtest")

    args = parser.parse_args()

    client = anthropic.Anthropic()

    if args.company:
        # Backtest mode: generate recommendation from company data
        print(f"Running backtest for {args.company}...")
        backtest = run_backtest(args.company, client=client)
        recommendation = backtest["recommendation"]

        # Load data for judge eval
        company_dir = DATA_DIR / "output" / args.company
        extractions = []
        for call in [1, 2, 3]:
            ext_path = company_dir / f"extraction_call{call}.json"
            if ext_path.exists():
                extractions.append(json.loads(ext_path.read_text()))

        memo = ""
        for v in [3, 2, 1]:
            candidate = company_dir / f"memo_v{v}.md"
            if candidate.exists():
                memo = candidate.read_text()
                break

        # Programmatic evals
        prog_results = run_programmatic_evals(recommendation)

        # Judge evals
        judge_scores = None
        if not args.skip_judge:
            print("\nRunning LLM judge...")
            judge_scores = run_judge_evals(extractions, memo, recommendation, client=client)

        _print_results(prog_results, judge_scores, backtest)

        # Save recommendation
        rec_path = company_dir / "recommendation.json"
        rec_path.write_text(json.dumps(recommendation, indent=2, ensure_ascii=False))
        print(f"\nRecommendation saved to {rec_path}")

    elif args.recommendation:
        # Evaluate existing recommendation
        if not args.extractions or not args.memo:
            parser.error("--extractions and --memo required with --recommendation")

        recommendation = json.loads(Path(args.recommendation).read_text())
        extractions = [json.loads(Path(p).read_text()) for p in args.extractions]
        memo = Path(args.memo).read_text()

        prog_results = run_programmatic_evals(recommendation)

        judge_scores = None
        if not args.skip_judge:
            print("\nRunning LLM judge...")
            judge_scores = run_judge_evals(extractions, memo, recommendation, client=client)

        _print_results(prog_results, judge_scores)

    else:
        parser.error("Provide --company or --recommendation")


if __name__ == "__main__":
    main()
