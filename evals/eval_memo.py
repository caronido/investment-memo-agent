"""Memo evaluation suite.

Programmatic evals: section presence, non-empty checks, TBD alignment with gap
analysis, deal term consistency.
LLM judge: completeness, factual accuracy, analytical quality, template compliance.

CLI:
    python -m evals.eval_memo --memo data/output/memo.md --extraction data/output/extraction.json
    python -m evals.eval_memo --memo data/output/memo.md --extraction data/output/extraction.json --gap-analysis data/output/gap.json
    python -m evals.eval_memo --all
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic

from evals.judges.memo_judge import judge_memo

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
BASELINES_PATH = Path(__file__).resolve().parent / "baselines.json"

# All 13 expected section titles from the memo template
EXPECTED_SECTIONS = [
    "Executive Summary",
    "Investment Thesis",
    "Team & Founders",
    "Problem Statement",
    "Product & Technology",
    "Business Model",
    "Market Analysis",
    "GTM Strategy",
    "Competitive Landscape",
    "Traction & Metrics",
    "Financial Review",
    "Concerns & Challenges",
    "Scoring Rubric",
]

# Sections that should have non-TBD content per call stage
# (sections whose updated_by_calls includes this stage)
SECTIONS_WITH_DATA = {
    1: [
        "Executive Summary", "Investment Thesis", "Team & Founders",
        "Problem Statement", "Business Model", "Market Analysis",
        "Traction & Metrics", "Concerns & Challenges",
    ],
    2: [
        "Executive Summary", "Investment Thesis", "Problem Statement",
        "Product & Technology", "Business Model", "Market Analysis",
        "Competitive Landscape", "Concerns & Challenges",
    ],
    3: [
        "Executive Summary", "Investment Thesis", "GTM Strategy",
        "Business Model", "Competitive Landscape", "Traction & Metrics",
        "Financial Review", "Concerns & Challenges", "Scoring Rubric",
    ],
}


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: str


def _extract_sections(memo: str) -> dict[str, str]:
    """Parse memo into section_title -> content dict."""
    sections = {}
    current_title = None
    current_lines = []

    for line in memo.split("\n"):
        if line.startswith("## "):
            if current_title:
                sections[current_title] = "\n".join(current_lines).strip()
            current_title = line[3:].strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)

    if current_title:
        sections[current_title] = "\n".join(current_lines).strip()

    return sections


def _find_numbers(text: str) -> list[str]:
    """Extract number-like strings from text (for consistency checks)."""
    return re.findall(r'\$[\d,.]+[MKBk]?\s*(?:MXN|USD)?', text)


def run_programmatic_evals(
    memo: str,
    extraction: dict,
    gap_analysis: dict | None = None,
) -> list[EvalResult]:
    """Run deterministic programmatic checks on a memo.

    Args:
        memo: The Markdown memo text.
        extraction: The extraction the memo was based on.
        gap_analysis: Optional gap analysis for TBD alignment checks.

    Returns:
        List of EvalResult with pass/fail for each check.
    """
    results = []
    sections = _extract_sections(memo)
    call_stage = extraction.get("call_stage", 1)

    # 1. All 13 sections present as ## headers
    missing_sections = []
    for expected in EXPECTED_SECTIONS:
        # Flexible match: check if expected title appears in any section key
        found = any(
            expected.lower() in key.lower()
            for key in sections
        )
        if not found:
            missing_sections.append(expected)
    results.append(EvalResult(
        name="all_sections_present",
        passed=len(missing_sections) == 0,
        details=f"Missing: {missing_sections}" if missing_sections else f"All {len(EXPECTED_SECTIONS)} sections present",
    ))

    # 2. Memo header present (# Investment Memo: ...)
    has_header = memo.strip().startswith("# ")
    results.append(EvalResult(
        name="memo_header",
        passed=has_header,
        details="Header present" if has_header else "Missing # header",
    ))

    # 3. Non-TBD content in sections that should have data
    if call_stage in SECTIONS_WITH_DATA:
        expected_with_data = SECTIONS_WITH_DATA[call_stage]
        empty_sections = []
        for sec_title in expected_with_data:
            # Find matching section
            content = None
            for key, val in sections.items():
                if sec_title.lower() in key.lower():
                    content = val
                    break
            if content is None:
                empty_sections.append(f"{sec_title} (missing)")
                continue
            # Check if section is purely TBD
            stripped = re.sub(r'\[TBD[^\]]*\]', '', content).strip()
            if not stripped:
                empty_sections.append(f"{sec_title} (only TBD)")
        results.append(EvalResult(
            name="sections_with_content",
            passed=len(empty_sections) == 0,
            details=f"Empty/TBD-only: {empty_sections}" if empty_sections else f"All {len(expected_with_data)} expected sections have content",
        ))

    # 4. TBD alignment with gap analysis coverage
    if gap_analysis:
        coverage = gap_analysis.get("coverage_summary", {})
        gap_sections = coverage.get("sections", [])
        none_sections = {
            s["section_name"] for s in gap_sections
            if s.get("coverage") == "none"
        }
        misaligned = []
        for sec_title, content in sections.items():
            has_tbd = "[tbd" in content.lower()
            # Find matching gap section
            sec_id = sec_title.lower().replace(" & ", "_").replace(" ", "_")
            is_none_coverage = any(
                ns in sec_id or sec_id in ns
                for ns in none_sections
            )
            # If gap says "none" coverage but memo has substantial non-TBD content, that's suspicious
            # (not a failure, but worth noting)
            # If gap says data exists but memo is all TBD, that's a problem
            if not is_none_coverage and not has_tbd:
                pass  # Good: data expected and content provided
            elif is_none_coverage and has_tbd:
                pass  # Good: no data and TBD marker present
        # Simpler check: sections flagged as "none" should contain [TBD]
        missing_tbd = []
        for ns in none_sections:
            for sec_title, content in sections.items():
                sec_id = sec_title.lower().replace(" & ", "_").replace(" ", "_")
                if ns in sec_id or sec_id in ns:
                    if "[tbd" not in content.lower():
                        missing_tbd.append(sec_title)
        results.append(EvalResult(
            name="tbd_alignment",
            passed=len(missing_tbd) == 0,
            details=f"Missing [TBD] in no-data sections: {missing_tbd}" if missing_tbd else "TBD markers aligned with gap analysis",
        ))

    # 5. Deal term consistency: key numbers in extraction should appear in memo
    deal_terms = []
    rd = extraction.get("round_dynamics", {})
    if isinstance(rd, dict):
        for field in ["raising_amount", "valuation", "nido_check_size"]:
            val = rd.get(field)
            if val and isinstance(val, str):
                # Extract just the numbers for matching
                nums = re.findall(r'[\d,.]+', val)
                deal_terms.extend(nums[:3])  # first few numbers
    bm = extraction.get("business_model", {})
    if isinstance(bm, dict):
        pricing = bm.get("pricing")
        if pricing and isinstance(pricing, str):
            nums = re.findall(r'[\d,.]+', pricing)
            deal_terms.extend(nums[:2])

    if deal_terms:
        memo_text = memo.replace(",", "")  # normalize commas
        missing_terms = []
        for term in deal_terms:
            normalized = term.replace(",", "")
            if normalized not in memo_text:
                missing_terms.append(term)
        # Allow some flexibility — at least half should appear
        pass_rate = 1 - (len(missing_terms) / len(deal_terms)) if deal_terms else 1
        results.append(EvalResult(
            name="deal_term_consistency",
            passed=pass_rate >= 0.5,
            details=f"{len(deal_terms) - len(missing_terms)}/{len(deal_terms)} key numbers present in memo" + (f" (missing: {missing_terms})" if missing_terms else ""),
        ))

    # 6. Company name appears in memo
    company = extraction.get("company", {})
    company_name = company.get("name", "") if isinstance(company, dict) else ""
    if company_name:
        name_in_memo = company_name.lower() in memo.lower()
        results.append(EvalResult(
            name="company_name_present",
            passed=name_in_memo,
            details=f"'{company_name}' found in memo" if name_in_memo else f"'{company_name}' not found in memo",
        ))

    # 7. Scoring rubric has at least one numeric score
    rubric_content = ""
    for key, val in sections.items():
        if "scoring" in key.lower() or "rubric" in key.lower():
            rubric_content = val
            break
    has_scores = bool(re.search(r'[1-5]/5', rubric_content))
    results.append(EvalResult(
        name="rubric_has_scores",
        passed=has_scores,
        details="Numeric scores found in rubric" if has_scores else "No N/5 scores in rubric section",
    ))

    return results


def run_judge_evals(
    extraction: dict,
    memo: str,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Run LLM-as-judge evaluation on a memo."""
    return judge_memo(extraction, memo, client=client)


def _discover_memos() -> list[dict]:
    """Find all memo files in data/output/."""
    output_dir = DATA_DIR / "output"
    results = []
    if not output_dir.exists():
        return results

    for company_dir in sorted(output_dir.iterdir()):
        if not company_dir.is_dir():
            continue
        for memo_file in sorted(company_dir.glob("memo_v*.md")):
            # Parse version from filename: memo_v1.md -> version 1
            version_str = memo_file.stem.replace("memo_v", "")
            try:
                version = int(version_str)
            except ValueError:
                continue
            # Find matching extraction
            ext_file = company_dir / f"extraction_call{version}.json"
            gap_file = company_dir / f"gap_analysis_call{version}.json"
            if ext_file.exists():
                entry = {
                    "company": company_dir.name,
                    "version": version,
                    "memo_path": memo_file,
                    "extraction_path": ext_file,
                }
                if gap_file.exists():
                    entry["gap_path"] = gap_file
                results.append(entry)
    return results


def run_all_evals() -> list[dict]:
    """Discover all memos and run the full eval suite."""
    entries = _discover_memos()
    if not entries:
        print("No memo files found in data/output/")
        return []

    client = anthropic.Anthropic()
    summaries = []

    for entry in entries:
        print(f"\n{'='*60}")
        print(f"Evaluating: {entry['company']} memo v{entry['version']}")
        print(f"{'='*60}")

        memo = entry["memo_path"].read_text()
        extraction = json.loads(entry["extraction_path"].read_text())
        gap_analysis = None
        if "gap_path" in entry:
            gap_analysis = json.loads(entry["gap_path"].read_text())

        # Programmatic evals
        prog_results = run_programmatic_evals(memo, extraction, gap_analysis)
        passed = sum(1 for r in prog_results if r.passed)
        total = len(prog_results)
        print(f"\n  Programmatic: {passed}/{total} passed")
        for r in prog_results:
            status = "PASS" if r.passed else "FAIL"
            print(f"    [{status}] {r.name}: {r.details}")

        # Judge evals
        print(f"\n  Running LLM judge...")
        judge_scores = run_judge_evals(extraction, memo, client=client)
        print(f"  Judge scores:")
        for dim in ["completeness", "factual_accuracy", "analytical_quality", "template_compliance"]:
            s = judge_scores[dim]
            print(f"    {dim}: {s['score']}/5 — {s['reasoning']}")
        print(f"  Overall: {judge_scores['overall_score']}/5")

        summaries.append({
            "company": entry["company"],
            "version": entry["version"],
            "programmatic_passed": passed,
            "programmatic_total": total,
            "completeness": judge_scores["completeness"]["score"],
            "factual_accuracy": judge_scores["factual_accuracy"]["score"],
            "analytical_quality": judge_scores["analytical_quality"]["score"],
            "template_compliance": judge_scores["template_compliance"]["score"],
            "overall_score": judge_scores["overall_score"],
        })

    _print_summary_table(summaries)
    _update_baselines(summaries)

    return summaries


def _print_summary_table(summaries: list[dict]):
    """Print formatted summary table."""
    print(f"\n{'='*85}")
    print("SUMMARY")
    print(f"{'='*85}")

    header = f"{'Memo':<20} {'Prog':>8} {'Comp':>5} {'Fact':>5} {'Anal':>5} {'Tmpl':>5} {'Overall':>7}"
    print(header)
    print("-" * len(header))

    for s in summaries:
        name = f"{s['company']}_v{s['version']}"
        prog = f"{s['programmatic_passed']}/{s['programmatic_total']}"
        print(
            f"{name:<20} {prog:>8} {s['completeness']:>5} "
            f"{s['factual_accuracy']:>5} {s['analytical_quality']:>5} "
            f"{s['template_compliance']:>5} {s['overall_score']:>7}"
        )


def _update_baselines(summaries: list[dict]):
    """Write or update baselines.json with best memo scores."""
    baselines = {}
    if BASELINES_PATH.exists():
        baselines = json.loads(BASELINES_PATH.read_text())

    for s in summaries:
        key = f"memo_{s['company']}_v{s['version']}"
        existing = baselines.get(key, {})
        baselines[key] = {
            "version": s["version"],
            "programmatic_passed": max(s["programmatic_passed"], existing.get("programmatic_passed", 0)),
            "programmatic_total": s["programmatic_total"],
            "completeness": max(s["completeness"], existing.get("completeness", 0)),
            "factual_accuracy": max(s["factual_accuracy"], existing.get("factual_accuracy", 0)),
            "analytical_quality": max(s["analytical_quality"], existing.get("analytical_quality", 0)),
            "template_compliance": max(s["template_compliance"], existing.get("template_compliance", 0)),
            "overall_score": max(s["overall_score"], existing.get("overall_score", 0)),
        }

    BASELINES_PATH.write_text(json.dumps(baselines, indent=2) + "\n")
    print(f"\nBaselines written to {BASELINES_PATH}")


def main():
    parser = argparse.ArgumentParser(
        description="Run memo evaluation suite."
    )
    parser.add_argument("--memo", help="Path to memo .md file")
    parser.add_argument("--extraction", help="Path to extraction JSON file")
    parser.add_argument("--gap-analysis", help="Path to gap analysis JSON file")
    parser.add_argument("--all", action="store_true", help="Run evals on all memos in data/output/")

    args = parser.parse_args()

    if not args.all and not args.memo:
        parser.error("Provide --memo or --all")

    if args.all:
        run_all_evals()
        return

    if not args.extraction:
        parser.error("--extraction required with --memo")

    memo_path = Path(args.memo)
    extraction_path = Path(args.extraction)
    if not memo_path.exists():
        print(f"Error: Memo not found: {memo_path}", file=sys.stderr)
        sys.exit(1)
    if not extraction_path.exists():
        print(f"Error: Extraction not found: {extraction_path}", file=sys.stderr)
        sys.exit(1)

    memo = memo_path.read_text()
    extraction = json.loads(extraction_path.read_text())

    gap_analysis = None
    if args.gap_analysis:
        gap_path = Path(args.gap_analysis)
        if not gap_path.exists():
            print(f"Error: Gap analysis not found: {gap_path}", file=sys.stderr)
            sys.exit(1)
        gap_analysis = json.loads(gap_path.read_text())

    client = anthropic.Anthropic()

    # Programmatic evals
    prog_results = run_programmatic_evals(memo, extraction, gap_analysis)
    passed = sum(1 for r in prog_results if r.passed)
    total = len(prog_results)
    print(f"\nProgrammatic: {passed}/{total} passed")
    for r in prog_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.name}: {r.details}")

    # Judge evals
    print(f"\nRunning LLM judge...")
    judge_scores = run_judge_evals(extraction, memo, client=client)
    print(f"Judge scores:")
    for dim in ["completeness", "factual_accuracy", "analytical_quality", "template_compliance"]:
        s = judge_scores[dim]
        print(f"  {dim}: {s['score']}/5 — {s['reasoning']}")
    print(f"Overall: {judge_scores['overall_score']}/5")


if __name__ == "__main__":
    main()
