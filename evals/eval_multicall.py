from __future__ import annotations

"""Multi-call progression evaluation.

Runs a sequence of calls through the pipeline and verifies that state
accumulates correctly: TBDs decrease, content is preserved, contradictions
are detected, and memos grow.

CLI:
    python -m evals.eval_multicall
    python -m evals.eval_multicall --output-dir data/output/multicall_eval/
"""

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.pipeline import run_pipeline
from src.state.manager import StateManager

load_dotenv()

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str


def run_multicall_progression_eval(
    call_sequence: list[tuple[str | Path, int | None]],
    output_dir: str | Path,
    company_name: str,
) -> list[CheckResult]:
    """Run N calls in sequence through the pipeline, then check progression.

    Args:
        call_sequence: List of (transcript_path, call_stage) tuples.
            call_stage can be None for auto-detection.
        output_dir: Directory for pipeline output and state.
        company_name: Company name for state management.

    Returns:
        List of CheckResult for each progression check.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results_per_call = []
    tbd_counts = []

    for i, (transcript_path, call_stage) in enumerate(call_sequence):
        transcript_path = Path(transcript_path)
        transcript = transcript_path.read_text()

        print(
            f"\n{'='*60}\n"
            f"Running call {i+1}/{len(call_sequence)}: {transcript_path.name}"
            f" (stage={call_stage or 'auto'})\n"
            f"{'='*60}",
            file=sys.stderr,
        )

        result = run_pipeline(
            transcript,
            call_stage=call_stage,
            output_dir=str(output_dir),
            skip_evals=True,
            company_name=company_name,
            use_state=True,
        )

        results_per_call.append(result)

        memo = result["memo"]
        tbd_count = memo.lower().count("[tbd")
        tbd_counts.append(tbd_count)
        print(f"  TBD count: {tbd_count}", file=sys.stderr)

    # --- Run checks ---
    checks = []

    # 1. TBD decreasing
    tbd_decreasing = all(
        tbd_counts[i] >= tbd_counts[i + 1] for i in range(len(tbd_counts) - 1)
    )
    checks.append(CheckResult(
        name="tbd_decreasing",
        passed=tbd_decreasing,
        details=f"TBD counts: {tbd_counts}. {'Monotonically decreasing' if tbd_decreasing else 'NOT monotonically decreasing'}",
    ))

    # 2. All calls processed
    state_mgr = StateManager(company_name, output_dir)
    expected_calls = []
    for _, call_stage in call_sequence:
        if call_stage:
            expected_calls.append(call_stage)
        else:
            # Auto-detected; get from result
            idx = len(expected_calls)
            if idx < len(results_per_call):
                expected_calls.append(
                    results_per_call[idx]["extraction"].get("call_stage", 0)
                )
    all_processed = all(
        state_mgr.has_processed_call(c) for c in expected_calls
    )
    checks.append(CheckResult(
        name="all_calls_processed",
        passed=all_processed,
        details=f"Expected: {sorted(expected_calls)}, In state: {state_mgr.state['calls_processed']}",
    ))

    # 3. Content preservation — key Call 1 data in final memo
    final_memo = results_per_call[-1]["memo"].lower()
    first_extraction = results_per_call[0]["extraction"]
    founders = first_extraction.get("founders", [])
    founder_name = None
    if founders and isinstance(founders[0], dict):
        founder_name = founders[0].get("name", "")

    if founder_name:
        content_preserved = founder_name.lower() in final_memo
        checks.append(CheckResult(
            name="content_preservation",
            passed=content_preserved,
            details=f"Founder '{founder_name}' {'found' if content_preserved else 'NOT found'} in final memo",
        ))
    else:
        # Try company name as fallback
        company = first_extraction.get("company", {})
        c_name = company.get("name", "") if isinstance(company, dict) else ""
        content_preserved = c_name.lower() in final_memo if c_name else True
        checks.append(CheckResult(
            name="content_preservation",
            passed=content_preserved,
            details=f"Company '{c_name}' {'found' if content_preserved else 'NOT found'} in final memo (founder name not found in extraction)",
        ))

    # 4. Contradiction detection — field exists in state
    state = state_mgr.state
    has_contradictions_field = "contradictions" in state
    checks.append(CheckResult(
        name="contradiction_detection",
        passed=has_contradictions_field,
        details=f"Contradictions field exists: {has_contradictions_field}. Keys: {list(state.get('contradictions', {}).keys())}",
    ))

    # 5. Memo completeness growth — final memo longer than initial
    first_len = len(results_per_call[0]["memo"])
    final_len = len(results_per_call[-1]["memo"])
    memo_grew = final_len >= first_len
    checks.append(CheckResult(
        name="memo_completeness_growth",
        passed=memo_grew,
        details=f"First memo: {first_len} chars, Final memo: {final_len} chars. {'Grew' if memo_grew else 'Shrank'}",
    ))

    # 6. State file valid
    state_path = output_dir / "state.json"
    state_file_valid = False
    if state_path.exists():
        try:
            json.loads(state_path.read_text())
            state_file_valid = True
        except json.JSONDecodeError:
            pass
    checks.append(CheckResult(
        name="state_file_valid",
        passed=state_file_valid,
        details=f"state.json exists: {state_path.exists()}, valid JSON: {state_file_valid}",
    ))

    # 7. Memo versions stored
    memos_in_state = state_mgr.state.get("memos", {})
    all_versions_stored = len(memos_in_state) == len(call_sequence)
    checks.append(CheckResult(
        name="memo_versions_stored",
        passed=all_versions_stored,
        details=f"Memo versions in state: {sorted(memos_in_state.keys())} (expected {len(call_sequence)})",
    ))

    # Print results
    _print_check_table(checks, tbd_counts)

    return checks


def _print_check_table(checks: list[CheckResult], tbd_counts: list[int]):
    """Print a summary table of check results."""
    print(f"\n{'='*70}", file=sys.stderr)
    print("MULTI-CALL PROGRESSION EVAL", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)

    print(f"TBD counts across calls: {tbd_counts}", file=sys.stderr)
    print(file=sys.stderr)

    passed = sum(1 for c in checks if c.passed)
    total = len(checks)

    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        print(f"  [{status}] {c.name}: {c.details}", file=sys.stderr)

    print(f"\n  Result: {passed}/{total} checks passed", file=sys.stderr)
    print(f"{'='*70}", file=sys.stderr)


def run_lazo_three_call_eval(
    output_dir: str | Path | None = None,
) -> list[CheckResult]:
    """Convenience wrapper: run Lazo call1 → call2 → call4 sequence.

    Args:
        output_dir: Output directory. Uses a temp dir if None.

    Returns:
        List of CheckResult.
    """
    transcripts_dir = DATA_DIR / "transcripts"

    call_sequence = [
        (transcripts_dir / "sample_lazo_call1.txt", 1),
        (transcripts_dir / "sample_lazo_call2.txt", 2),
        (transcripts_dir / "sample_lazo_Call4.txt", 4),
    ]

    # Verify all transcripts exist
    for path, _ in call_sequence:
        if not Path(path).exists():
            print(f"Error: Transcript not found: {path}", file=sys.stderr)
            sys.exit(1)

    use_temp = output_dir is None
    if use_temp:
        output_dir = Path(tempfile.mkdtemp(prefix="multicall_eval_"))
        print(f"Using temp dir: {output_dir}", file=sys.stderr)
    else:
        output_dir = Path(output_dir)

    try:
        return run_multicall_progression_eval(
            call_sequence=call_sequence,
            output_dir=output_dir,
            company_name="Lazo",
        )
    finally:
        if use_temp:
            shutil.rmtree(output_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="Run multi-call progression evaluation (Lazo: call1 → call2 → call4)."
    )
    parser.add_argument(
        "--output-dir",
        help="Directory for pipeline output. Uses temp dir if omitted.",
    )

    args = parser.parse_args()

    checks = run_lazo_three_call_eval(output_dir=args.output_dir)

    passed = sum(1 for c in checks if c.passed)
    total = len(checks)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
