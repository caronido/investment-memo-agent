from __future__ import annotations

"""Slack Block Kit formatters for pipeline output.

Converts pipeline results (extraction, gap analysis, memo, eval report)
into Slack Block Kit message structures for rich formatting in threads.
"""

import json

# Slack block text limit is 3000 chars
_BLOCK_TEXT_LIMIT = 3000
# Slack message limit for file upload threshold
_MESSAGE_CHAR_LIMIT = 3900


def format_acknowledgment(company_name: str | None = None) -> list[dict]:
    """Format the initial acknowledgment message."""
    text = ":hourglass_flowing_sand: *Processing transcript"
    if company_name:
        text += f" for {company_name}"
    text += "...*\nThis may take 1-2 minutes. I'll post updates in this thread."
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


def format_extraction_summary(extraction: dict) -> list[dict]:
    """Format extraction results as a Block Kit summary.

    Shows company info, founders, key metrics, and field counts.
    """
    blocks = []
    call_stage = extraction.get("call_stage", "?")

    # Header
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"Stage 1/3: Extraction Complete (Call {call_stage})"},
    })

    # Company info
    company = extraction.get("company", {})
    if isinstance(company, dict):
        name = _get_value(company, "name", "Unknown")
        one_liner = _get_value(company, "one_liner", "")
        industry = _get_value(company, "industry", "")
        stage = _get_value(company, "stage", "")
        geo = _get_value(company, "geography", "")

        company_text = f"*{name}*"
        if one_liner:
            company_text += f"\n{one_liner}"
        details = []
        if industry:
            details.append(f":factory: {industry}")
        if stage:
            details.append(f":seedling: {stage}")
        if geo:
            details.append(f":earth_americas: {geo}")
        if details:
            company_text += "\n" + " | ".join(details)

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": company_text},
        })

    # Founders
    founders = extraction.get("founders", [])
    if founders:
        founder_lines = []
        for f in founders[:5]:  # Limit to 5
            if isinstance(f, dict):
                fname = _get_value(f, "name", "Unknown")
                role = _get_value(f, "role", "")
                line = f":bust_in_silhouette: *{fname}*"
                if role:
                    line += f" — {role}"
                founder_lines.append(line)
        if founder_lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(founder_lines)},
            })

    # Round dynamics
    rd = extraction.get("round_dynamics", {})
    if isinstance(rd, dict):
        rd_lines = []
        raising = _get_value(rd, "raising_amount")
        valuation = _get_value(rd, "valuation")
        instrument = _get_value(rd, "instrument")
        if raising:
            rd_lines.append(f":money_with_wings: Raising: {raising}")
        if valuation:
            rd_lines.append(f":chart_with_upwards_trend: Valuation: {valuation}")
        if instrument:
            rd_lines.append(f":page_facing_up: Instrument: {instrument}")
        if rd_lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(rd_lines)},
            })

    # Field count summary
    populated = _count_populated(extraction)
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f":white_check_mark: {populated} fields extracted"},
        ],
    })

    return blocks


def format_memo(memo: str, company_name: str | None = None) -> dict:
    """Format the memo for posting.

    Returns a dict with either 'blocks' (for short memos) or
    'file' (for memos that exceed Slack's message limit).
    """
    section_count = memo.count("\n## ")
    tbd_count = memo.lower().count("[tbd")

    header_text = ":memo: *Investment Memo Draft"
    if company_name:
        header_text += f" — {company_name}"
    header_text += f"*\n{section_count} sections, {tbd_count} TBD placeholders"

    if len(memo) <= _MESSAGE_CHAR_LIMIT:
        # Post as message blocks
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(memo, _BLOCK_TEXT_LIMIT)}},
        ]
        return {"blocks": blocks}

    # Too long — upload as file
    return {
        "file": {
            "content": memo,
            "filename": f"memo_{company_name or 'draft'}.md",
            "title": f"Investment Memo — {company_name or 'Draft'}",
            "initial_comment": header_text,
        }
    }


def format_gap_analysis(gap_analysis: dict) -> list[dict]:
    """Format gap analysis as a checklist of follow-up questions."""
    blocks = []

    questions = gap_analysis.get("follow_up_questions", [])
    doc_requests = gap_analysis.get("document_requests", [])

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "Stage 2/3: Gap Analysis"},
    })

    # Follow-up questions
    if questions:
        q_lines = []
        for q in questions[:15]:  # Limit
            if isinstance(q, dict):
                priority = q.get("priority", "medium")
                emoji = ":red_circle:" if priority == "high" else ":large_orange_circle:" if priority == "medium" else ":white_circle:"
                text = q.get("question", str(q))
            else:
                emoji = ":large_orange_circle:"
                text = str(q)
            q_lines.append(f"{emoji} {text}")

        q_text = "\n".join(q_lines)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Follow-up Questions ({len(questions)}):*\n{_truncate(q_text, _BLOCK_TEXT_LIMIT - 100)}"},
        })

    # Document requests
    if doc_requests:
        d_lines = []
        for d in doc_requests[:10]:
            if isinstance(d, dict):
                doc_name = d.get("document", str(d))
                reason = d.get("reason", "")
                line = f":file_folder: {doc_name}"
                if reason:
                    line += f" — _{reason}_"
            else:
                line = f":file_folder: {d}"
            d_lines.append(line)

        d_text = "\n".join(d_lines)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Document Requests ({len(doc_requests)}):*\n{_truncate(d_text, _BLOCK_TEXT_LIMIT - 100)}"},
        })

    if not questions and not doc_requests:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":white_check_mark: No gaps identified."},
        })

    return blocks


def format_eval_report(eval_report: dict) -> list[dict]:
    """Format eval scores as a compact summary."""
    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "Quality Report"},
    })

    summary = eval_report.get("summary", {})
    total_prog = summary.get("total_programmatic", "?")
    avg_judge = summary.get("avg_judge_score", "?")

    lines = [f"*Overall:* {total_prog} programmatic, {avg_judge}/5 judge avg"]

    for stage_name, stage_key in [("Extraction", "extraction"), ("Gap Analysis", "gap_analysis"), ("Memo", "memo")]:
        stage = eval_report.get(stage_key, {})
        prog_p = stage.get("programmatic_passed", 0)
        prog_t = stage.get("programmatic_total", 0)
        judge = stage.get("judge_scores", {}).get("overall_score", "?")
        lines.append(f"  {stage_name}: {prog_p}/{prog_t} prog, {judge}/5 judge")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    })

    return blocks


def format_error(error_msg: str) -> list[dict]:
    """Format an error message."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":x: *Error:* {error_msg}"},
        },
    ]


def format_pipeline_complete(result: dict) -> list[dict]:
    """Format a completion summary."""
    memo = result.get("memo", "")
    section_count = memo.count("\n## ")
    tbd_count = memo.lower().count("[tbd")
    contradictions = result.get("contradictions", [])

    lines = [":white_check_mark: *Pipeline complete!*"]
    lines.append(f"  Memo: {section_count} sections, {tbd_count} TBD placeholders, {len(memo):,} chars")

    if contradictions:
        lines.append(f"  :warning: {len(contradictions)} contradiction(s) with previous calls")

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
    ]


def build_transcript_modal(company_name: str | None = None) -> dict:
    """Build the modal view for pasting a transcript."""
    blocks = [
        {
            "type": "input",
            "block_id": "transcript_block",
            "label": {"type": "plain_text", "text": "Paste transcript"},
            "element": {
                "type": "plain_text_input",
                "action_id": "transcript_input",
                "multiline": True,
                "placeholder": {"type": "plain_text", "text": "Paste the full call transcript here..."},
            },
        },
        {
            "type": "input",
            "block_id": "company_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "Company name (optional, auto-detected)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "company_input",
                "placeholder": {"type": "plain_text", "text": "e.g., Lazo"},
            },
        },
        {
            "type": "input",
            "block_id": "call_stage_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "Call stage (optional, auto-detected)"},
            "element": {
                "type": "static_select",
                "action_id": "call_stage_input",
                "placeholder": {"type": "plain_text", "text": "Auto-detect"},
                "options": [
                    {"text": {"type": "plain_text", "text": "Call 1 — Founder Story"}, "value": "1"},
                    {"text": {"type": "plain_text", "text": "Call 2 — Product Deep Dive"}, "value": "2"},
                    {"text": {"type": "plain_text", "text": "Call 3 — GTM Validation"}, "value": "3"},
                    {"text": {"type": "plain_text", "text": "Call 4 — Other"}, "value": "4"},
                ],
            },
        },
        {
            "type": "input",
            "block_id": "evals_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "Run quality evals?"},
            "element": {
                "type": "static_select",
                "action_id": "evals_input",
                "initial_option": {"text": {"type": "plain_text", "text": "Skip (faster)"}, "value": "skip"},
                "options": [
                    {"text": {"type": "plain_text", "text": "Skip (faster)"}, "value": "skip"},
                    {"text": {"type": "plain_text", "text": "Run evals"}, "value": "run"},
                ],
            },
        },
    ]

    title = "Process Transcript"

    return {
        "type": "modal",
        "callback_id": "transcript_modal",
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Run Pipeline"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def format_status(company_name: str, state: dict) -> list[dict]:
    """Format company memo status from state data."""
    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"Memo Status: {company_name}"},
    })

    calls = state.get("calls_processed", [])
    memos = state.get("memos", {})
    extractions = state.get("extractions", {})

    if not calls:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":information_source: No calls processed yet for this company."},
        })
        return blocks

    # Calls processed
    call_labels = {1: "Founder Story", 2: "Product Deep Dive", 3: "GTM Validation", 4: "Other"}
    call_lines = []
    for c in sorted(calls):
        label = call_labels.get(c, f"Call {c}")
        call_lines.append(f":white_check_mark: Call {c} — {label}")
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Calls Processed:*\n" + "\n".join(call_lines)},
    })

    # Latest memo stats
    if memos:
        latest_key = max(memos.keys(), key=int)
        latest_memo = memos[latest_key]
        section_count = latest_memo.count("\n## ")
        tbd_count = latest_memo.lower().count("[tbd")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"*Latest Memo (v{latest_key}):*\n"
                f"  {section_count} sections, {tbd_count} TBD placeholders, {len(latest_memo):,} chars"
            )},
        })

    # Contradictions summary
    contradictions = state.get("contradictions", {})
    total_contradictions = sum(len(v) for v in contradictions.values() if isinstance(v, list))
    if total_contradictions:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":warning: {total_contradictions} contradiction(s) detected across calls"},
        })

    # Timestamps
    created = state.get("created_at", "")[:10]
    updated = state.get("updated_at", "")[:10]
    if created:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Created: {created} | Last updated: {updated}"},
            ],
        })

    return blocks


def format_questions(company_name: str, state: dict) -> list[dict]:
    """Format the latest gap analysis questions for a company."""
    blocks = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"Open Questions: {company_name}"},
    })

    gap_analyses = state.get("gap_analyses", {})
    if not gap_analyses:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":information_source: No gap analysis available yet. Run the pipeline first."},
        })
        return blocks

    # Collect questions from all calls
    all_questions = []
    all_doc_requests = []
    for call_key in sorted(gap_analyses.keys(), key=int):
        gap = gap_analyses[call_key]
        for q in gap.get("follow_up_questions", []):
            if isinstance(q, dict):
                q["_from_call"] = int(call_key)
                all_questions.append(q)
        for d in gap.get("document_requests", []):
            if isinstance(d, dict):
                d["_from_call"] = int(call_key)
                all_doc_requests.append(d)

    if all_questions:
        q_lines = []
        for q in all_questions[:20]:
            priority = q.get("priority", "medium")
            emoji = ":red_circle:" if priority == "high" else ":large_orange_circle:" if priority == "medium" else ":white_circle:"
            call = q.get("_from_call", "?")
            text = q.get("question", str(q))
            q_lines.append(f"{emoji} _(Call {call})_ {text}")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Follow-up Questions ({len(all_questions)}):*\n{_truncate(chr(10).join(q_lines), _BLOCK_TEXT_LIMIT - 100)}"},
        })

    if all_doc_requests:
        d_lines = []
        for d in all_doc_requests[:10]:
            doc_name = d.get("document", str(d))
            call = d.get("_from_call", "?")
            d_lines.append(f":file_folder: _(Call {call})_ {doc_name}")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Document Requests ({len(all_doc_requests)}):*\n" + "\n".join(d_lines)},
        })

    if not all_questions and not all_doc_requests:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":white_check_mark: No open questions or document requests."},
        })

    return blocks


def format_attio_company(company: dict) -> list[dict]:
    """Format an Attio company lookup result."""
    name = company.get("name", "Unknown")
    web_url = company.get("web_url", "")
    transcripts = company.get("transcripts", [])

    text = f":mag: Found *{name}* in Attio"
    if web_url:
        text += f"\n<{web_url}|View in Attio>"
    text += f"\n:page_facing_up: {len(transcripts)} transcript(s) found"

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


def format_multi_call_progress(idx: int, total: int, title: str) -> list[dict]:
    """Format a progress message for multi-call processing."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":hourglass: Processing transcript {idx}/{total}: *{title}*"},
        },
    ]


def format_call_skipped(call_stage: int) -> list[dict]:
    """Format a message when a call stage is skipped (already processed)."""
    call_labels = {1: "Founder Story", 2: "Product Deep Dive", 3: "GTM Validation", 4: "Other"}
    label = call_labels.get(call_stage, f"Call {call_stage}")
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":fast_forward: Call {call_stage} ({label}) already processed, skipping"},
        },
    ]


def format_google_doc_link(doc_url: str, title: str) -> list[dict]:
    """Format a Google Doc link message."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":page_facing_up: Google Doc created: <{doc_url}|{title}>"},
        },
    ]


def format_deck_progress(source: str) -> list[dict]:
    """Format a progress message for deck fetching."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":page_facing_up: Fetching deck from {source}..."},
        },
    ]


def format_deck_enriched(stats: dict) -> list[dict]:
    """Format a message after deck enrichment completes."""
    new_fields = stats.get("new_fields", 0)
    updated_fields = stats.get("updated_fields", 0)
    parts = []
    if new_fields:
        parts.append(f"+{new_fields} new fields")
    if updated_fields:
        parts.append(f"{updated_fields} updated")
    detail = ", ".join(parts) if parts else "no new fields"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":white_check_mark: Deck enriched extraction: {detail}"},
        },
    ]


def format_attio_writeback(fields: list[str]) -> list[dict]:
    """Format a message after writing data back to Attio."""
    if not fields:
        return []
    field_list = ", ".join(fields)
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":arrows_counterclockwise: Updated Attio: {field_list}"},
        },
    ]


def format_deal_summary(deal: dict) -> list[dict]:
    """Format a summary of deal data from Attio."""
    lines = [":briefcase: *Deal Data from Attio:*"]

    field_labels = {
        "sector": "Sector",
        "funding_round": "Funding Round",
        "target_raise": "Target Raise",
        "initial_round_valuation_cap": "Valuation/Cap",
        "deal_stage": "Deal Stage",
        "deal_quality": "Deal Quality",
        "source": "Source",
    }

    for field, label in field_labels.items():
        val = deal.get(field)
        if val is not None and val != "":
            if isinstance(val, (int, float)):
                # Format currency values
                if val >= 1_000_000:
                    lines.append(f"  {label}: ${val / 1_000_000:.1f}M")
                elif val >= 1_000:
                    lines.append(f"  {label}: ${val / 1_000:.0f}K")
                else:
                    lines.append(f"  {label}: {val}")
            else:
                lines.append(f"  {label}: {val}")

    if len(lines) == 1:
        return []  # No deal data to show

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        },
    ]


def format_document_checklist(
    documents: list[dict],
    folder_url: str | None = None,
) -> list[dict]:
    """Format a document list with processed/unprocessed status.

    Each doc in documents should have: name (str), processed (bool), source (str).

    Args:
        documents: List of document dicts.
        folder_url: Optional link to the Drive folder.

    Returns:
        Block Kit blocks showing the checklist.
    """
    if not documents:
        return []

    lines = [":file_folder: *Documents:*"]
    for doc in documents:
        name = doc.get("name", "Unknown")
        processed = doc.get("processed", False)
        source = doc.get("source", "")
        emoji = ":white_check_mark:" if processed else ":x:"
        source_label = f" ({source})" if source else ""
        lines.append(f"{emoji} {name}{source_label}")

    if folder_url:
        lines.append(f":link: <{folder_url}|Open Drive folder>")

    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines), _BLOCK_TEXT_LIMIT)},
        },
    ]


def format_recommendation(recommendation: dict) -> list[dict]:
    """Format an investment recommendation as Block Kit blocks.

    Shows recommendation decision, confidence badge, 6 dimension scores,
    and overall rationale.
    """
    blocks = []

    rec_value = recommendation.get("recommendation", "?")
    confidence = recommendation.get("confidence_score", 0)
    overall_score = recommendation.get("overall_score", 0)

    # Decision emoji
    emoji_map = {"INVEST": ":white_check_mark:", "PASS": ":no_entry_sign:", "REVISIT": ":hourglass:"}
    emoji = emoji_map.get(rec_value, ":question:")

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"Investment Recommendation: {rec_value}"},
    })

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": (
            f"{emoji} *{rec_value}* | Confidence: *{confidence}%* | Overall: *{overall_score}/5*"
        )},
    })

    # Rubric scores
    rubric = recommendation.get("rubric", {})
    dimension_labels = {
        "team": "Team",
        "market": "Market",
        "product": "Product",
        "business_model": "Business Model",
        "traction": "Traction",
        "competition": "Competition",
    }
    score_lines = []
    for dim_key, label in dimension_labels.items():
        dim = rubric.get(dim_key, {})
        score = dim.get("score", "?")
        rationale = dim.get("rationale", "")
        # Truncate rationale for compact display
        if len(rationale) > 120:
            rationale = rationale[:117] + "..."
        bar = _score_bar(score)
        score_lines.append(f"{bar} *{label}* ({score}/5): {rationale}")

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": _truncate("\n".join(score_lines), _BLOCK_TEXT_LIMIT)},
    })

    # Overall rationale
    overall_rationale = recommendation.get("overall_rationale", "")
    if overall_rationale:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Rationale:*\n{_truncate(overall_rationale, _BLOCK_TEXT_LIMIT - 50)}"},
        })

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": "_This is a draft recommendation for human review. It is not an investment decision._"},
        ],
    })

    return blocks


def _score_bar(score) -> str:
    """Convert a 1-5 score to a visual bar."""
    try:
        s = int(score)
    except (ValueError, TypeError):
        return ":white_circle:" * 5
    filled = ":large_green_circle:" if s >= 4 else ":large_orange_circle:" if s >= 3 else ":red_circle:"
    return filled


def format_no_company(company_name: str) -> list[dict]:
    """Format a 'company not found' message."""
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f":mag: *{company_name}* not found in Attio.\n"
                "Use `/memo` without a company name to paste a transcript directly."
            )},
        },
    ]


# --- Helpers ---

def _get_value(obj: dict, key: str, default: str = "") -> str:
    """Get a value from a dict, handling attributed {value, source} dicts."""
    val = obj.get(key)
    if val is None:
        return default
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    if val is None:
        return default
    return str(val)


def _truncate(text: str, limit: int) -> str:
    """Truncate text to a limit, adding ellipsis if needed."""
    if len(text) <= limit:
        return text
    return text[:limit - 20] + "\n\n_...truncated..._"


def _count_populated(extraction: dict) -> int:
    """Count non-null leaf fields in an extraction."""
    count = 0
    for key, val in extraction.items():
        if key.startswith("_") or key in ("sources", "call_stage"):
            continue
        if isinstance(val, dict):
            for _, v in val.items():
                if v is not None and v != "" and v != []:
                    count += 1
        elif isinstance(val, list):
            count += len(val)
        elif val is not None and val != "":
            count += 1
    return count
