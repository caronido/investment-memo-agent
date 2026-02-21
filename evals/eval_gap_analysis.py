"""Gap analysis evaluation suite.

Three eval types:
1. Programmatic: schema compliance, non-empty checks, coverage score validation,
   stage-appropriate question targeting, document request alignment
2. LLM judge: specificity, stage-appropriateness, usefulness via Claude Haiku
3. Combined runner with summary output

CLI:
    python -m evals.eval_gap_analysis --extraction data/output/extraction.json --gap-analysis data/output/gap_analysis.json
    python -m evals.eval_gap_analysis --all
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic

from evals.judges.gap_judge import judge_gap_analysis
from src.gap_analysis.analyzer import analyze_gaps

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
BASELINES_PATH = Path(__file__).resolve().parent / "baselines.json"

# Valid coverage values per the gap_analysis.json schema
VALID_COVERAGE = {"none", "partial", "sufficient", "complete"}

# Valid priority values
VALID_PRIORITIES = {"critical", "important", "nice_to_have"}

# Valid severity values
VALID_SEVERITIES = {"low", "medium", "high"}

# Memo sections from the template
MEMO_SECTION_IDS = {
    "executive_summary", "investment_thesis", "team_founders",
    "problem_statement", "product_technology", "business_model",
    "market_analysis", "gtm_strategy", "competitive_landscape",
    "traction_metrics", "financial_review", "concerns_challenges",
    "scoring_rubric",
}

# Expected question focus areas per call stage (what the NEXT call covers)
NEXT_CALL_FOCUS = {
    1: {  # After Call 1 -> questions should target Call 2 topics
        "expected_sections": {
            "product_technology", "business_model", "problem_statement",
            "competitive_landscape", "market_analysis",
        },
        "description": "product/tech/unit economics for Call 2",
    },
    2: {  # After Call 2 -> questions should target Call 3 topics
        "expected_sections": {
            "gtm_strategy", "traction_metrics", "financial_review",
            "competitive_landscape", "concerns_challenges",
        },
        "description": "GTM/sales/financials for Call 3",
    },
    3: {  # After Call 3 -> only remaining gaps
        "expected_sections": MEMO_SECTION_IDS,  # any section is valid
        "description": "remaining open items only",
    },
    4: {  # After Call 4 -> any section
        "expected_sections": MEMO_SECTION_IDS,
        "description": "any remaining gaps",
    },
}

# Expected document keyword signals per call stage (from the three-call process).
# We check if any of these keywords appear in the doc request text.
# Each inner list is OR'd — matching any keyword in the group counts as a hit.
EXPECTED_DOCS_PER_STAGE = {
    1: [  # After Call 1: request these for Call 2
        ["cap table", "equity", "capitalization"],
        ["financial model", "financial projection", "unit economics"],
        ["market siz", "tam", "sam"],
        ["competit", "landscape"],
        ["incorporat", "legal structure"],
    ],
    2: [  # After Call 2: request these for Call 3
        ["tech memo", "technical", "architecture"],
        ["roadmap", "product plan"],
    ],
    3: [  # After Call 3: request remaining
        ["client list", "customer list", "reference"],
        ["sales metric", "cac", "ltv", "churn"],
    ],
}


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: str


def run_programmatic_evals(
    gap_analysis: dict,
    call_stage: int,
) -> list[EvalResult]:
    """Run deterministic checks on gap analysis output.

    Args:
        gap_analysis: The gap analysis dict to evaluate.
        call_stage: Which call was just completed.

    Returns:
        List of EvalResult with pass/fail for each check.
    """
    results = []

    # 1. Required top-level fields
    required = ["call_stage", "company_name", "coverage_summary", "follow_up_questions"]
    missing = [f for f in required if f not in gap_analysis]
    results.append(EvalResult(
        name="required_fields",
        passed=len(missing) == 0,
        details=f"Missing: {missing}" if missing else "All required fields present",
    ))

    # 2. Follow-up questions non-empty (should always have questions after calls 1-2)
    questions = gap_analysis.get("follow_up_questions", [])
    if call_stage in (1, 2):
        results.append(EvalResult(
            name="questions_non_empty",
            passed=len(questions) > 0,
            details=f"{len(questions)} questions" if questions else "No questions generated (expected some after call {call_stage})",
        ))
    else:
        # After call 3, it's ok to have few or no questions
        results.append(EvalResult(
            name="questions_non_empty",
            passed=True,
            details=f"{len(questions)} questions (post-call-3, may be few)",
        ))

    # 3. Question structure: each must have question, priority, memo_section
    q_structure_errors = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            q_structure_errors.append(f"Q{i}: not a dict")
            continue
        if "question" not in q or not q["question"]:
            q_structure_errors.append(f"Q{i}: missing question text")
        if q.get("priority") not in VALID_PRIORITIES:
            q_structure_errors.append(f"Q{i}: invalid priority '{q.get('priority')}'")
        if "memo_section" not in q or not q["memo_section"]:
            q_structure_errors.append(f"Q{i}: missing memo_section")
    results.append(EvalResult(
        name="question_structure",
        passed=len(q_structure_errors) == 0,
        details="; ".join(q_structure_errors) if q_structure_errors else f"All {len(questions)} questions well-structured",
    ))

    # 4. Coverage summary structure
    coverage = gap_analysis.get("coverage_summary", {})
    sections = coverage.get("sections", [])
    cov_errors = []
    for sec in sections:
        if not isinstance(sec, dict):
            cov_errors.append("section entry is not a dict")
            continue
        if "section_name" not in sec:
            cov_errors.append("missing section_name")
        if sec.get("coverage") not in VALID_COVERAGE:
            cov_errors.append(f"{sec.get('section_name', '?')}: invalid coverage '{sec.get('coverage')}'")
    results.append(EvalResult(
        name="coverage_structure",
        passed=len(cov_errors) == 0,
        details="; ".join(cov_errors) if cov_errors else f"All {len(sections)} sections valid",
    ))

    # 5. Stage-appropriate question targeting
    if call_stage in NEXT_CALL_FOCUS and questions:
        focus = NEXT_CALL_FOCUS[call_stage]
        expected = focus["expected_sections"]
        on_target = sum(1 for q in questions if q.get("memo_section") in expected)
        ratio = on_target / len(questions) if questions else 0
        # At least 60% of questions should target the right sections
        results.append(EvalResult(
            name="stage_targeting",
            passed=ratio >= 0.6,
            details=f"{on_target}/{len(questions)} ({ratio:.0%}) target {focus['description']}",
        ))

    # 6. Document requests structure
    doc_requests = gap_analysis.get("document_requests", [])
    doc_errors = []
    for i, d in enumerate(doc_requests):
        if not isinstance(d, dict):
            doc_errors.append(f"Doc{i}: not a dict")
            continue
        if "document" not in d or not d["document"]:
            doc_errors.append(f"Doc{i}: missing document name")
        if d.get("priority") not in VALID_PRIORITIES:
            doc_errors.append(f"Doc{i}: invalid priority '{d.get('priority')}'")
        if "reason" not in d or not d["reason"]:
            doc_errors.append(f"Doc{i}: missing reason")
    results.append(EvalResult(
        name="doc_request_structure",
        passed=len(doc_errors) == 0,
        details="; ".join(doc_errors) if doc_errors else f"All {len(doc_requests)} doc requests well-structured",
    ))

    # 7. Document request alignment with expected docs per stage
    if call_stage in EXPECTED_DOCS_PER_STAGE and doc_requests:
        expected_groups = EXPECTED_DOCS_PER_STAGE[call_stage]
        doc_text = " ".join(
            d.get("document", "").lower() + " " + d.get("reason", "").lower()
            for d in doc_requests
        )
        matched_groups = []
        for group in expected_groups:
            if any(keyword in doc_text for keyword in group):
                matched_groups.append(group[0])  # use first keyword as label
        # At least one expected doc type should be requested
        results.append(EvalResult(
            name="doc_alignment",
            passed=len(matched_groups) > 0,
            details=f"Matched {len(matched_groups)}/{len(expected_groups)} expected doc types: {matched_groups}",
        ))

    # 8. Data quality flags structure (optional field but check if present)
    flags = gap_analysis.get("data_quality_flags", [])
    flag_errors = []
    for i, f in enumerate(flags):
        if not isinstance(f, dict):
            flag_errors.append(f"Flag{i}: not a dict")
            continue
        if f.get("severity") not in VALID_SEVERITIES:
            flag_errors.append(f"Flag{i}: invalid severity '{f.get('severity')}'")
        if "field" not in f or not f["field"]:
            flag_errors.append(f"Flag{i}: missing field")
        if "issue" not in f or not f["issue"]:
            flag_errors.append(f"Flag{i}: missing issue")
    if flags:
        results.append(EvalResult(
            name="data_quality_flags_structure",
            passed=len(flag_errors) == 0,
            details="; ".join(flag_errors) if flag_errors else f"All {len(flags)} flags well-structured",
        ))

    return results


def run_judge_evals(
    extraction: dict,
    gap_analysis: dict,
    call_stage: int,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run LLM-as-judge evaluation on gap analysis output.

    Returns:
        Dict with specificity, stage_appropriateness, usefulness scores and overall_score.
    """
    return judge_gap_analysis(extraction, gap_analysis, call_stage, client=client)


def _discover_gap_analyses() -> list[dict]:
    """Find all gap analysis files in data/output/."""
    output_dir = DATA_DIR / "output"
    results = []
    if not output_dir.exists():
        return results

    for company_dir in sorted(output_dir.iterdir()):
        if not company_dir.is_dir():
            continue
        for gap_file in sorted(company_dir.glob("gap_analysis_call*.json")):
            # Find matching extraction
            call_num = gap_file.stem.replace("gap_analysis_call", "")
            ext_file = company_dir / f"extraction_call{call_num}.json"
            if ext_file.exists():
                results.append({
                    "company": company_dir.name,
                    "call_stage": int(call_num),
                    "gap_path": gap_file,
                    "extraction_path": ext_file,
                })
    return results


def run_all_evals() -> list[dict]:
    """Discover all gap analyses and run the full eval suite."""
    entries = _discover_gap_analyses()
    if not entries:
        print("No gap analysis files found in data/output/")
        return []

    client = anthropic.Anthropic()
    summaries = []

    for entry in entries:
        print(f"\n{'='*60}")
        print(f"Evaluating: {entry['company']} call {entry['call_stage']}")
        print(f"{'='*60}")

        extraction = json.loads(entry["extraction_path"].read_text())
        gap_analysis = json.loads(entry["gap_path"].read_text())
        call_stage = entry["call_stage"]

        # Programmatic evals
        prog_results = run_programmatic_evals(gap_analysis, call_stage)
        passed = sum(1 for r in prog_results if r.passed)
        total = len(prog_results)
        print(f"\n  Programmatic: {passed}/{total} passed")
        for r in prog_results:
            status = "PASS" if r.passed else "FAIL"
            print(f"    [{status}] {r.name}: {r.details}")

        # Judge evals
        print(f"\n  Running LLM judge...")
        judge_scores = run_judge_evals(extraction, gap_analysis, call_stage, client=client)
        print(f"  Judge scores:")
        for dim in ["specificity", "stage_appropriateness", "usefulness"]:
            s = judge_scores[dim]
            print(f"    {dim}: {s['score']}/5 — {s['reasoning']}")
        print(f"  Overall: {judge_scores['overall_score']}/5")

        summaries.append({
            "company": entry["company"],
            "call_stage": call_stage,
            "programmatic_passed": passed,
            "programmatic_total": total,
            "specificity": judge_scores["specificity"]["score"],
            "stage_appropriateness": judge_scores["stage_appropriateness"]["score"],
            "usefulness": judge_scores["usefulness"]["score"],
            "overall_score": judge_scores["overall_score"],
        })

    _print_summary_table(summaries)
    _update_baselines(summaries)

    return summaries


def _print_summary_table(summaries: list[dict]):
    """Print formatted summary table."""
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    header = f"{'Company':<20} {'Call':>4} {'Prog':>8} {'Spec':>5} {'Stage':>5} {'Use':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for s in summaries:
        prog = f"{s['programmatic_passed']}/{s['programmatic_total']}"
        print(
            f"{s['company']:<20} {s['call_stage']:>4} {prog:>8} "
            f"{s['specificity']:>5} {s['stage_appropriateness']:>5} "
            f"{s['usefulness']:>5} {s['overall_score']:>7}"
        )


def _update_baselines(summaries: list[dict]):
    """Write or update baselines.json with best gap analysis scores."""
    baselines = {}
    if BASELINES_PATH.exists():
        baselines = json.loads(BASELINES_PATH.read_text())

    for s in summaries:
        key = f"gap_{s['company']}_call{s['call_stage']}"
        existing = baselines.get(key, {})
        baselines[key] = {
            "call_stage": s["call_stage"],
            "programmatic_passed": max(
                s["programmatic_passed"],
                existing.get("programmatic_passed", 0),
            ),
            "programmatic_total": s["programmatic_total"],
            "specificity": max(s["specificity"], existing.get("specificity", 0)),
            "stage_appropriateness": max(
                s["stage_appropriateness"],
                existing.get("stage_appropriateness", 0),
            ),
            "usefulness": max(s["usefulness"], existing.get("usefulness", 0)),
            "overall_score": max(s["overall_score"], existing.get("overall_score", 0)),
        }

    BASELINES_PATH.write_text(json.dumps(baselines, indent=2) + "\n")
    print(f"\nBaselines written to {BASELINES_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Run gap analysis evaluation suite."
    )
    parser.add_argument(
        "--extraction",
        help="Path to extraction JSON file",
    )
    parser.add_argument(
        "--gap-analysis",
        help="Path to gap analysis JSON file (if omitted, runs gap analysis fresh)",
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        help="Call stage. Auto-detected from extraction if omitted.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run evals on all gap analyses in data/output/",
    )

    args = parser.parse_args()

    if not args.all and not args.extraction:
        parser.error("Provide --extraction or --all")

    if args.all:
        run_all_evals()
        return

    # Single evaluation mode
    extraction_path = Path(args.extraction)
    if not extraction_path.exists():
        print(f"Error: Extraction not found: {extraction_path}", file=sys.stderr)
        sys.exit(1)

    extraction = json.loads(extraction_path.read_text())

    # Determine call stage
    call_stage = args.call_stage
    if call_stage is None:
        call_stage = extraction.get("call_stage")
        if call_stage is None:
            print("Error: --call-stage not provided and extraction has no call_stage field", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-detected call stage: {call_stage}")

    client = anthropic.Anthropic()

    # Load or generate gap analysis
    if args.gap_analysis:
        gap_path = Path(args.gap_analysis)
        if not gap_path.exists():
            print(f"Error: Gap analysis not found: {gap_path}", file=sys.stderr)
            sys.exit(1)
        gap_analysis = json.loads(gap_path.read_text())
        print(f"Loaded gap analysis from {gap_path}")
    else:
        print(f"Running gap analysis for call stage {call_stage}...")
        gap_analysis = analyze_gaps(extraction, call_stage, client=client)

    # Run programmatic evals
    prog_results = run_programmatic_evals(gap_analysis, call_stage)
    passed = sum(1 for r in prog_results if r.passed)
    total = len(prog_results)
    print(f"\nProgrammatic: {passed}/{total} passed")
    for r in prog_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.details}")

    # Run judge evals
    print(f"\nRunning LLM judge...")
    judge_scores = run_judge_evals(extraction, gap_analysis, call_stage, client=client)
    print(f"Judge scores:")
    for dim in ["specificity", "stage_appropriateness", "usefulness"]:
        s = judge_scores[dim]
        print(f"  {dim}: {s['score']}/5 — {s['reasoning']}")
    print(f"Overall: {judge_scores['overall_score']}/5")


if __name__ == "__main__":
    main()
