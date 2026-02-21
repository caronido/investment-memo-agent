from __future__ import annotations

"""Merge transcript and document extractions with source attribution.

Combines data from multiple sources (transcript, deck, financial model),
keeping track of where each field value came from. When both sources provide
the same field, flags discrepancies.

Usage:
    merged = merge_extractions(transcript_ext, document_ext)
    # merged["company"]["name"] -> {"value": "Lazo", "source": "transcript", "page": null}
    # merged["_discrepancies"] -> [{"field": "round_dynamics.valuation", ...}]
"""

import json
from copy import deepcopy
from datetime import datetime, timezone


def _is_empty(value) -> bool:
    """Check if a value is effectively empty."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _normalize_for_comparison(value) -> str:
    """Normalize a value for comparison (lowercase, strip formatting)."""
    import re
    s = str(value).strip().lower()
    s = re.sub(r'[$,\s]', '', s)
    return s


_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}


def _parse_numeric(s: str) -> float | None:
    """Parse a numeric string, handling k/m/b suffixes."""
    import re
    s = re.sub(r'[$,\s]', '', s.strip().lower())
    if not s:
        return None
    for suffix, multiplier in _SUFFIXES.items():
        if s.endswith(suffix):
            try:
                return float(s[:-1]) * multiplier
            except ValueError:
                pass
    try:
        return float(s)
    except ValueError:
        return None


def _values_match(val_a, val_b) -> bool:
    """Check if two values are effectively the same."""
    if _is_empty(val_a) or _is_empty(val_b):
        return True  # Can't conflict if one is empty

    norm_a = _normalize_for_comparison(val_a)
    norm_b = _normalize_for_comparison(val_b)

    if norm_a == norm_b:
        return True

    # Substring containment
    if norm_a and norm_b:
        if norm_a in norm_b or norm_b in norm_a:
            return True

    # Numeric comparison (handles $10M vs $10,000,000)
    num_a = _parse_numeric(str(val_a))
    num_b = _parse_numeric(str(val_b))
    if num_a is not None and num_b is not None and num_a == num_b:
        return True

    # Legacy float fallback
    try:
        if float(norm_a) == float(norm_b):
            return True
    except (ValueError, TypeError):
        pass

    return False


def _get_source_for_field(field_path: str, sources: list[dict]) -> dict | None:
    """Find the source entry for a given field path."""
    for src in sources:
        if isinstance(src, dict) and src.get("field") == field_path:
            return src
    return None


def _merge_scalar_field(
    field_path: str,
    transcript_val,
    document_val,
    transcript_sources: list[dict],
    document_sources: list[dict],
) -> tuple[dict, dict | None]:
    """Merge a single scalar field from two sources.

    Returns:
        Tuple of (attributed_value, discrepancy_or_None).
        attributed_value has keys: value, source, page (if from deck).
    """
    t_empty = _is_empty(transcript_val)
    d_empty = _is_empty(document_val)

    discrepancy = None

    if t_empty and d_empty:
        return {"value": None, "source": None}, None

    if t_empty and not d_empty:
        # Only document has data
        doc_src = _get_source_for_field(field_path, document_sources)
        page = doc_src.get("page") if doc_src else None
        return {"value": document_val, "source": "deck", "page": page}, None

    if not t_empty and d_empty:
        # Only transcript has data
        return {"value": transcript_val, "source": "transcript"}, None

    # Both have data
    if _values_match(transcript_val, document_val):
        # Consistent — prefer transcript (it's the primary source)
        return {"value": transcript_val, "source": "transcript"}, None

    # Discrepancy — keep transcript value but flag the conflict
    doc_src = _get_source_for_field(field_path, document_sources)
    page = doc_src.get("page") if doc_src else None
    discrepancy = {
        "field": field_path,
        "transcript_value": str(transcript_val),
        "document_value": str(document_val),
        "document_page": page,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"value": transcript_val, "source": "transcript", "also_in": "deck"}, discrepancy


def _merge_object(
    prefix: str,
    transcript_obj: dict | None,
    document_obj: dict | None,
    transcript_sources: list[dict],
    document_sources: list[dict],
    discrepancies: list[dict],
) -> dict:
    """Recursively merge two dicts, attributing each field."""
    transcript_obj = transcript_obj or {}
    document_obj = document_obj or {}

    all_keys = set(list(transcript_obj.keys()) + list(document_obj.keys()))
    merged = {}

    for key in sorted(all_keys):
        field_path = f"{prefix}.{key}" if prefix else key
        t_val = transcript_obj.get(key)
        d_val = document_obj.get(key)

        # Skip internal/meta keys
        if key.startswith("_"):
            merged[key] = t_val if t_val is not None else d_val
            continue

        # Both are dicts — recurse
        if isinstance(t_val, dict) and not isinstance(t_val.get("value"), str):
            if isinstance(d_val, dict) and not isinstance(d_val.get("value"), str):
                merged[key] = _merge_object(
                    field_path, t_val, d_val,
                    transcript_sources, document_sources, discrepancies,
                )
                continue

        # Both are lists — merge arrays
        if isinstance(t_val, list) and isinstance(d_val, list):
            merged[key] = _merge_arrays(
                field_path, t_val, d_val,
                transcript_sources, document_sources,
            )
            continue

        # Scalar merge
        attributed, disc = _merge_scalar_field(
            field_path, t_val, d_val,
            transcript_sources, document_sources,
        )
        merged[key] = attributed
        if disc:
            discrepancies.append(disc)

    return merged


def _merge_arrays(
    field_path: str,
    transcript_arr: list,
    document_arr: list,
    transcript_sources: list[dict],
    document_sources: list[dict],
) -> list:
    """Merge two arrays, deduplicating where possible.

    For arrays of objects (like founders), uses name-based matching.
    For arrays of strings, does set-union with source tagging.
    """
    if not transcript_arr and not document_arr:
        return []

    if not transcript_arr:
        return [{"value": item, "source": "deck"} for item in document_arr]

    if not document_arr:
        return [{"value": item, "source": "transcript"} for item in transcript_arr]

    # Arrays of objects (e.g., founders, key_metrics)
    if transcript_arr and isinstance(transcript_arr[0], dict):
        # Tag each with source and return combined (dedup is hard generically)
        result = [{"value": item, "source": "transcript"} for item in transcript_arr]

        # Add document items that don't overlap
        t_names = set()
        for item in transcript_arr:
            if isinstance(item, dict) and "name" in item:
                t_names.add(_normalize_for_comparison(item["name"]))

        for item in document_arr:
            if isinstance(item, dict) and "name" in item:
                if _normalize_for_comparison(item["name"]) not in t_names:
                    result.append({"value": item, "source": "deck"})
            else:
                result.append({"value": item, "source": "deck"})

        return result

    # Arrays of scalars (e.g., concerns, document_requests)
    t_set = {_normalize_for_comparison(v) for v in transcript_arr}
    result = [{"value": item, "source": "transcript"} for item in transcript_arr]
    for item in document_arr:
        if _normalize_for_comparison(item) not in t_set:
            result.append({"value": item, "source": "deck"})
    return result


# Fields to merge at the top level as objects (recurse into them)
_OBJECT_FIELDS = {
    "company", "round_dynamics", "business_model", "traction", "market",
    "problem_statement", "product", "technology", "unit_economics",
    "competitive_landscape", "gtm_strategy", "traction_metrics",
    "stickiness", "competitive_strategy", "financial_review",
}

# Fields to merge as arrays
_ARRAY_FIELDS = {
    "founders", "concerns", "document_requests",
}

# Fields to skip during merge (handled separately)
_SKIP_FIELDS = {
    "call_stage", "sources", "_source_document",
}


def merge_extractions(
    transcript_extraction: dict,
    document_extraction: dict,
) -> dict:
    """Merge transcript and document extractions with source attribution.

    Args:
        transcript_extraction: Extraction from transcript (primary source).
        document_extraction: Extraction from document (enrichment source).

    Returns:
        Merged dict with source attribution on each field:
        - Scalar fields: {"value": ..., "source": "transcript"|"deck", "page": N}
        - Array fields: [{"value": ..., "source": "transcript"|"deck"}, ...]
        - Object fields: nested attributed dicts
        - _discrepancies: list of conflicts between sources
        - _sources: combined source list with attribution
        - _enrichment_stats: {transcript_only, document_only, both, discrepancies}
    """
    t_sources = transcript_extraction.get("sources", [])
    d_sources = document_extraction.get("sources", [])

    discrepancies = []

    merged = {}

    # Preserve call_stage from transcript
    merged["call_stage"] = transcript_extraction.get("call_stage", 1)

    # Merge object fields recursively
    for field in _OBJECT_FIELDS:
        t_val = transcript_extraction.get(field)
        d_val = document_extraction.get(field)
        if t_val is not None or d_val is not None:
            merged[field] = _merge_object(
                field, t_val, d_val, t_sources, d_sources, discrepancies,
            )

    # Merge array fields
    for field in _ARRAY_FIELDS:
        t_val = transcript_extraction.get(field, [])
        d_val = document_extraction.get(field, [])
        if t_val or d_val:
            merged[field] = _merge_arrays(
                field, t_val or [], d_val or [], t_sources, d_sources,
            )

    # Merge remaining scalar fields
    all_keys = set(
        list(transcript_extraction.keys()) + list(document_extraction.keys())
    )
    for field in sorted(all_keys):
        if field in merged or field in _SKIP_FIELDS or field in _OBJECT_FIELDS or field in _ARRAY_FIELDS:
            continue
        t_val = transcript_extraction.get(field)
        d_val = document_extraction.get(field)
        attributed, disc = _merge_scalar_field(
            field, t_val, d_val, t_sources, d_sources,
        )
        merged[field] = attributed
        if disc:
            discrepancies.append(disc)

    # Combine sources with type attribution
    combined_sources = []
    for src in t_sources:
        if isinstance(src, dict):
            src_copy = deepcopy(src)
            src_copy.setdefault("source_type", "transcript")
            combined_sources.append(src_copy)
    for src in d_sources:
        if isinstance(src, dict):
            src_copy = deepcopy(src)
            src_copy.setdefault("source_type", "deck")
            combined_sources.append(src_copy)
    merged["sources"] = combined_sources

    # Metadata
    merged["_discrepancies"] = discrepancies
    merged["_source_document"] = document_extraction.get("_source_document")
    merged["_enrichment_stats"] = _compute_enrichment_stats(
        transcript_extraction, document_extraction, merged,
    )

    return merged


def _count_populated_fields(data: dict, prefix: str = "") -> set[str]:
    """Count non-empty leaf fields in an extraction dict."""
    populated = set()
    for key, val in data.items():
        if key.startswith("_") or key in ("sources", "call_stage"):
            continue
        field_path = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            populated.update(_count_populated_fields(val, field_path))
        elif not _is_empty(val):
            populated.add(field_path)
    return populated


def _compute_enrichment_stats(
    transcript_ext: dict,
    document_ext: dict,
    merged: dict,
) -> dict:
    """Compute stats about how much the document enriched the extraction."""
    t_fields = _count_populated_fields(transcript_ext)
    d_fields = _count_populated_fields(document_ext)

    transcript_only = t_fields - d_fields
    document_only = d_fields - t_fields
    both = t_fields & d_fields

    return {
        "transcript_fields": len(t_fields),
        "document_fields": len(d_fields),
        "transcript_only": len(transcript_only),
        "document_only": len(document_only),
        "both": len(both),
        "total_unique_fields": len(t_fields | d_fields),
    }
