from __future__ import annotations

"""Per-company state persistence for multi-call pipeline.

Tracks extractions, gap analyses, memos, and contradictions across calls.
State is stored as JSON in {output_dir}/state.json.

Usage:
    mgr = StateManager("Lazo", "data/output/lazo/")
    mgr.add_call_result(1, extraction, gap_analysis, memo, contradictions)
    mgr.save()

    previous = mgr.get_previous_extractions(before_call=2)
    latest_memo = mgr.get_latest_memo()
"""

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


class StateManager:
    """JSON-backed per-company state manager."""

    def __init__(self, company_name: str, output_dir: str | Path):
        self.company_name = company_name
        self.output_dir = Path(output_dir)
        self._state_path = self.output_dir / "state.json"
        self._state = self._load_or_init()

    def _load_or_init(self) -> dict:
        """Load existing state from disk or initialize fresh."""
        if self._state_path.exists():
            data = json.loads(self._state_path.read_text())
            # Update company name if provided
            if self.company_name:
                data["company_name"] = self.company_name
            return data

        return {
            "company_name": self.company_name,
            "calls_processed": [],
            "extractions": {},
            "gap_analyses": {},
            "memos": {},
            "contradictions": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def save(self):
        """Write state to disk."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False)
        )

    def add_call_result(
        self,
        call_stage: int,
        extraction: dict,
        gap_analysis: dict,
        memo: str,
        contradictions: list[dict] | None = None,
    ):
        """Store results from a pipeline run and save to disk.

        Args:
            call_stage: The call number (1-4).
            extraction: Extraction JSON for this call.
            gap_analysis: Gap analysis JSON for this call.
            memo: Memo markdown string after this call.
            contradictions: List of contradiction dicts (if any).
        """
        key = str(call_stage)

        if call_stage not in self._state["calls_processed"]:
            self._state["calls_processed"].append(call_stage)
            self._state["calls_processed"].sort()

        self._state["extractions"][key] = extraction
        self._state["gap_analyses"][key] = gap_analysis
        self._state["memos"][key] = memo

        if contradictions:
            self._state["contradictions"][key] = contradictions

        self.save()

    def get_previous_extractions(self, before_call: int) -> list[dict]:
        """Return extractions from calls before the given stage.

        Args:
            before_call: Call stage to look before (exclusive).

        Returns:
            List of extraction dicts from earlier calls, sorted by call stage.
        """
        result = []
        for key, extraction in self._state["extractions"].items():
            if int(key) < before_call:
                result.append(extraction)
        result.sort(key=lambda e: e.get("call_stage", 0))
        return result

    def get_latest_memo(self) -> str | None:
        """Return the most recent memo string, or None if no memos exist."""
        memos = self._state["memos"]
        if not memos:
            return None
        latest_key = max(memos.keys(), key=int)
        return memos[latest_key]

    def has_processed_call(self, call_stage: int) -> bool:
        """Check if a call stage has already been processed."""
        return call_stage in self._state["calls_processed"]

    @property
    def state(self) -> dict:
        """Return a copy of the full state dict."""
        return deepcopy(self._state)


def _get_nested_value(data: dict, dot_path: str):
    """Get a value from a nested dict using dot notation.

    Args:
        data: The dict to traverse.
        dot_path: Dot-separated path like "company.name" or "round_dynamics.valuation".

    Returns:
        The value at the path, or None if not found.
    """
    keys = dot_path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _values_compatible(val_a, val_b) -> bool:
    """Check if two values are compatible (fuzzy match).

    Handles currency formatting ($, commas), numeric equivalence,
    and substring containment for strings.
    """
    if val_a is None or val_b is None:
        return True  # Can't contradict if one is missing

    # Normalize to strings for comparison
    str_a = str(val_a).strip()
    str_b = str(val_b).strip()

    if str_a == str_b:
        return True

    # Strip currency/formatting chars and compare
    cleaned_a = re.sub(r'[$,\s]', '', str_a).lower()
    cleaned_b = re.sub(r'[$,\s]', '', str_b).lower()

    if cleaned_a == cleaned_b:
        return True

    # Substring containment (e.g. "Lazo" in "Lazo Technologies")
    if cleaned_a and cleaned_b:
        if cleaned_a in cleaned_b or cleaned_b in cleaned_a:
            return True

    # Try numeric comparison
    try:
        num_a = float(cleaned_a)
        num_b = float(cleaned_b)
        if num_a == num_b:
            return True
    except (ValueError, TypeError):
        pass

    return False


# Fields that should remain consistent across calls
_CONSISTENCY_FIELDS = [
    "company.name",
    "company.founded_year",
    "round_dynamics.raising_amount",
    "round_dynamics.valuation",
    "round_dynamics.instrument",
]


def detect_contradictions(
    new_extraction: dict,
    previous_extractions: list[dict],
    call_stage: int,
) -> list[dict]:
    """Detect contradictions between new extraction and previous ones.

    Compares fields that should remain consistent across calls.

    Args:
        new_extraction: The current call's extraction.
        previous_extractions: List of extractions from prior calls.
        call_stage: The current call stage.

    Returns:
        List of contradiction dicts with: field, call_X_value, call_Y_value,
        severity, detected_at.
    """
    contradictions = []

    for prev in previous_extractions:
        prev_stage = prev.get("call_stage", "?")

        for field in _CONSISTENCY_FIELDS:
            new_val = _get_nested_value(new_extraction, field)
            prev_val = _get_nested_value(prev, field)

            if not _values_compatible(new_val, prev_val):
                contradictions.append({
                    "field": field,
                    f"call_{prev_stage}_value": str(prev_val),
                    f"call_{call_stage}_value": str(new_val),
                    "severity": "high" if "valuation" in field or "raising" in field else "medium",
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })

    return contradictions
