from __future__ import annotations

"""Slack bot for the memo-agent pipeline.

Listens for /memo slash commands, opens a modal for transcript input,
runs the pipeline asynchronously, and posts results in a thread.

Uses Slack Socket Mode for local development.

CLI:
    python -m src.slack.app
"""

import json
import logging
import os
import threading
import traceback
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.extraction.extractor import detect_call_theme, THEME_NAMES
from src.integrations.attio import AttioClient, is_configured as attio_configured
from src.pipeline import run_pipeline
from src.slack.formatters import (
    build_transcript_modal,
    format_acknowledgment,
    format_attio_company,
    format_attio_writeback,
    format_call_skipped,
    format_deal_summary,
    format_deck_enriched,
    format_deck_progress,
    format_document_checklist,
    format_error,
    format_eval_report,
    format_extraction_summary,
    format_gap_analysis,
    format_google_doc_link,
    format_memo,
    format_multi_call_progress,
    format_no_company,
    format_pipeline_complete,
    format_questions,
    format_status,
)
from src.slack.parser import (
    find_company_dir,
    get_output_dir,
    parse_memo_command,
    validate_transcript,
)
from src.state import StateManager

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Shared Anthropic client for all pipeline runs
_anthropic_client = None


def _get_client() -> anthropic.Anthropic:
    """Get or create the shared Anthropic client."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


def create_app() -> App:
    """Create and configure the Slack Bolt app with all handlers.

    Deferred initialization avoids import-time errors when tokens
    aren't set (e.g., during testing or CI).
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN not set. Set it in .env or export it."
        )
    bolt_app = App(
        token=token,
        signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    )
    _register_handlers(bolt_app)
    return bolt_app


def _register_handlers(bolt_app: App):
    """Register all slash command, view, and event handlers."""

    @bolt_app.command("/memo")
    def handle_memo_command(ack, command, client):
        """Handle /memo slash command.

        Dispatches based on parsed input:
        - /memo                     → open transcript paste modal
        - /memo domain.com status   → show current memo state
        - /memo domain.com questions→ show missing questions
        - /memo domain.com          → search Attio by domain, pull transcripts, run pipeline
        """
        ack()

        parsed = parse_memo_command(command.get("text", ""))
        company_name = parsed.get("company_name")
        subcommand = parsed.get("subcommand")
        call_stage = parsed.get("call_stage")
        channel_id = command["channel_id"]
        user_id = command["user_id"]

        # No company → open modal for manual transcript paste
        if not company_name:
            _open_transcript_modal(client, command, parsed)
            return

        # --- Subcommands: status / questions ---
        if subcommand in ("status", "questions"):
            _handle_subcommand(client, channel_id, company_name, subcommand)
            return

        # --- Company name provided: try Attio lookup ---
        if attio_configured():
            try:
                attio = AttioClient()
                company = attio.search_and_get_company(company_name)

                if company and company.get("transcripts"):
                    # Found company with transcripts — run pipeline directly
                    transcripts = company["transcripts"]
                    resolved_name = company.get("name", company_name)
                    deck_url = company.get("deck_url")
                    deal = company.get("deal")
                    record_id = company.get("record_id")

                    blocks = format_attio_company(company)
                    # Show deal data if available
                    if deal:
                        deal_blocks = format_deal_summary(deal)
                        blocks.extend(deal_blocks)

                    # --- Create/get deal folder & discover documents ---
                    deal_folder = None
                    doc_sources: list[dict] = []

                    try:
                        from src.integrations.google_docs import (
                            GoogleDocsClient,
                            is_configured as gdocs_configured,
                        )

                        if gdocs_configured():
                            gdocs = GoogleDocsClient()
                            deal_folder = gdocs.create_or_get_deal_folder(resolved_name)

                            # List files in the deal folder
                            folder_files = gdocs.list_folder_files(deal_folder["folder_id"])
                            for f in folder_files:
                                doc_sources.append({
                                    "file_id": f["file_id"],
                                    "name": f["name"],
                                    "mime_type": f.get("mime_type", ""),
                                    "source": "drive",
                                })
                    except Exception as e:
                        logger.warning("Deal folder setup failed: %s", e, exc_info=True)

                    # Scan Attio notes for document URLs
                    if record_id:
                        try:
                            note_urls = attio.extract_document_urls_from_notes(
                                record_id, exclude_url=deck_url,
                            )
                            for u in note_urls:
                                doc_sources.append({
                                    "url": u["url"],
                                    "name": u["url"].split("/")[-1] or u["url"],
                                    "source": "attio_note",
                                    "note_title": u.get("note_title"),
                                })
                        except Exception as e:
                            logger.warning("Attio note URL scan failed: %s", e)

                    # Build document checklist using state
                    unprocessed_doc_count = 0
                    if doc_sources:
                        output_dir = str(get_output_dir(resolved_name))
                        state_mgr = StateManager(resolved_name, output_dir)
                        checklist_docs = []
                        for ds in doc_sources:
                            is_processed = state_mgr.is_document_processed(ds["name"])
                            checklist_docs.append({
                                "name": ds["name"],
                                "processed": is_processed,
                                "source": ds.get("source", ""),
                            })
                            if not is_processed:
                                unprocessed_doc_count += 1
                        folder_url = deal_folder["folder_url"] if deal_folder else None
                        blocks.extend(format_document_checklist(checklist_docs, folder_url))

                    n = len(transcripts)
                    extra = ""
                    if deck_url:
                        extra = " + deck"
                    if unprocessed_doc_count:
                        extra += f" + {unprocessed_doc_count} doc(s)"
                    blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f":rocket: Creating investment memo and open questions from {n} transcript(s){extra}..."},
                    })
                    ack_msg = client.chat_postMessage(
                        channel=channel_id,
                        blocks=blocks,
                        text=f"Found {resolved_name} in Attio. Creating memo from {n} transcript(s)...",
                    )
                    thread_ts = ack_msg["ts"]

                    thread = threading.Thread(
                        target=_run_pipeline_async,
                        args=(client, channel_id, thread_ts, user_id),
                        kwargs={
                            "transcripts": transcripts,
                            "company_name": resolved_name,
                            "call_stage": call_stage,
                            "skip_evals": True,
                            "deck_url": deck_url,
                            "deal": deal,
                            "deal_folder": deal_folder,
                            "doc_sources": doc_sources,
                        },
                        daemon=True,
                    )
                    thread.start()
                    return

                elif company:
                    # Found company but no transcripts — open modal
                    client.chat_postMessage(
                        channel=channel_id,
                        blocks=format_attio_company(company),
                        text=f"Found {company.get('name')} in Attio but no transcripts.",
                    )
                    _open_transcript_modal(client, command, parsed)
                    return

            except Exception as e:
                logger.warning("Attio lookup failed: %s", e)
                # Fall through to modal

        # No Attio or company not found — open modal with company pre-filled
        _open_transcript_modal(client, command, parsed)

    @bolt_app.view("transcript_modal")
    def handle_modal_submission(ack, view, client):
        """Handle transcript modal submission — run the pipeline async."""
        ack()

        # Extract values from modal
        values = view["state"]["values"]

        transcript = (
            values.get("transcript_block", {})
            .get("transcript_input", {})
            .get("value", "")
        )

        company_name = (
            values.get("company_block", {})
            .get("company_input", {})
            .get("value")
        )

        call_stage_raw = (
            values.get("call_stage_block", {})
            .get("call_stage_input", {})
            .get("selected_option")
        )
        call_stage = int(call_stage_raw["value"]) if call_stage_raw else None

        run_evals_raw = (
            values.get("evals_block", {})
            .get("evals_input", {})
            .get("selected_option")
        )
        skip_evals = not (run_evals_raw and run_evals_raw.get("value") == "run")

        # Get metadata
        metadata = json.loads(view.get("private_metadata", "{}"))
        channel_id = metadata.get("channel_id")
        user_id = metadata.get("user_id")

        # Use call_stage from command if not set in modal
        if call_stage is None:
            call_stage = metadata.get("call_stage")

        if not channel_id:
            logger.error("No channel_id in modal metadata")
            return

        # Validate transcript
        error = validate_transcript(transcript)
        if error:
            client.chat_postMessage(
                channel=channel_id,
                blocks=format_error(error),
                text=error,
            )
            return

        # Post acknowledgment and get thread ts
        ack_msg = client.chat_postMessage(
            channel=channel_id,
            blocks=format_acknowledgment(company_name),
            text=f"Processing transcript{' for ' + company_name if company_name else ''}...",
        )
        thread_ts = ack_msg["ts"]

        # Run pipeline in background thread
        thread = threading.Thread(
            target=_run_pipeline_async,
            args=(client, channel_id, thread_ts, user_id, transcript),
            kwargs={
                "company_name": company_name,
                "call_stage": call_stage,
                "skip_evals": skip_evals,
            },
            daemon=True,
        )
        thread.start()

    @bolt_app.event("app_mention")
    def handle_mention(event, say):
        """Respond to @mentions with usage instructions."""
        say(
            text="Use `/memo` to process a transcript. I'll open a form for you to paste it.",
            thread_ts=event.get("ts"),
        )


# --- Helper functions ---


def _open_transcript_modal(client, command: dict, parsed: dict):
    """Open the transcript paste modal, optionally pre-filling company name."""
    modal = build_transcript_modal(company_name=parsed.get("company_name"))

    metadata = json.dumps({
        "channel_id": command["channel_id"],
        "user_id": command["user_id"],
        "company_name": parsed.get("company_name"),
        "call_stage": parsed.get("call_stage"),
    })
    modal["private_metadata"] = metadata

    if parsed.get("company_name"):
        for block in modal["blocks"]:
            if block.get("block_id") == "company_block":
                block["element"]["initial_value"] = parsed["company_name"]

    client.views_open(
        trigger_id=command["trigger_id"],
        view=modal,
    )


def _handle_subcommand(client, channel_id: str, company_name: str, subcommand: str):
    """Handle status/questions subcommands by reading local state."""
    company_dir = find_company_dir(company_name)

    if not company_dir:
        client.chat_postMessage(
            channel=channel_id,
            blocks=format_no_company(company_name),
            text=f"{company_name} not found.",
        )
        return

    try:
        mgr = StateManager(company_name, company_dir)
        state = mgr.state
    except Exception as e:
        logger.warning("Failed to load state for %s: %s", company_name, e)
        client.chat_postMessage(
            channel=channel_id,
            blocks=format_error(f"Could not load state for {company_name}: {e}"),
            text=f"Error loading state for {company_name}",
        )
        return

    if subcommand == "status":
        blocks = format_status(company_name, state)
    else:  # questions
        blocks = format_questions(company_name, state)

    client.chat_postMessage(
        channel=channel_id,
        blocks=blocks,
        text=f"{subcommand.title()} for {company_name}",
    )


# --- Async pipeline runner ---


def _run_pipeline_async(
    client,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    transcript: str | None = None,
    *,
    transcripts: list[dict] | None = None,
    company_name: str | None = None,
    call_stage: int | None = None,
    skip_evals: bool = True,
    deck_url: str | None = None,
    deal: dict | None = None,
    deal_folder: dict | None = None,
    doc_sources: list[dict] | None = None,
):
    """Run the pipeline in a background thread and post results to the thread.

    Supports two modes:
    - Single transcript (modal flow): pass ``transcript`` as a string.
    - Multi-transcript (Attio flow): pass ``transcripts`` as a list of note dicts.
      Transcripts are processed oldest-first; already-processed calls are skipped.

    Posts progress updates per call, then final results from the last run.
    """
    try:
        anthropic_client = _get_client()

        # Determine output directory
        output_dir = None
        if company_name:
            output_dir = str(get_output_dir(company_name))

        # --- Multi-transcript flow ---
        if transcripts:
            result = _run_multi_transcript_pipeline(
                client, channel_id, thread_ts, anthropic_client,
                transcripts=transcripts,
                company_name=company_name,
                call_stage=call_stage,
                output_dir=output_dir,
                skip_evals=skip_evals,
                deck_url=deck_url,
                deal=deal,
                deal_folder=deal_folder,
                doc_sources=doc_sources,
            )
        else:
            # --- Single-transcript flow (modal) ---
            result = run_pipeline(
                transcript,
                call_stage=call_stage,
                output_dir=output_dir,
                skip_evals=skip_evals,
                client=anthropic_client,
                company_name=company_name,
                use_state=True,
            )

        # Resolve company name from extraction if not provided
        if not company_name:
            ext_company = result.get("extraction", {}).get("company", {})
            if isinstance(ext_company, dict):
                company_name = ext_company.get("name")

        # Post final results from the last pipeline run
        _post_pipeline_results(client, channel_id, thread_ts, result, company_name, deal_folder=deal_folder)

    except anthropic.RateLimitError:
        _post_blocks(
            client, channel_id, thread_ts,
            format_error(
                "Claude API rate limit reached. Please wait a minute and try again."
            ),
        )
    except anthropic.APIError as e:
        _post_blocks(
            client, channel_id, thread_ts,
            format_error(f"Claude API error: {e.message}"),
        )
    except Exception as e:
        logger.exception("Pipeline failed")
        error_detail = str(e)
        if len(error_detail) > 500:
            error_detail = error_detail[:500] + "..."
        _post_blocks(
            client, channel_id, thread_ts,
            format_error(f"Pipeline failed: {error_detail}"),
        )


def _run_multi_transcript_pipeline(
    slack_client,
    channel_id: str,
    thread_ts: str,
    anthropic_client: anthropic.Anthropic,
    *,
    transcripts: list[dict],
    company_name: str | None,
    call_stage: int | None,
    output_dir: str | None,
    skip_evals: bool,
    deck_url: str | None = None,
    deal: dict | None = None,
    deal_folder: dict | None = None,
    doc_sources: list[dict] | None = None,
) -> dict:
    """Process multiple transcripts chronologically, skipping already-processed calls.

    Optionally fetches a deck PDF from ``deck_url`` and passes it to the first
    pipeline run.  Downloads unprocessed documents from Drive/Attio notes and
    passes them alongside the deck.  After all runs complete, writes extracted
    data back to Attio.

    Returns the result dict from the last pipeline run (the most complete memo).
    """
    import tempfile

    # Reverse to oldest-first (Attio returns newest-first)
    ordered = list(reversed(transcripts))
    total = len(ordered)

    # Load existing state to check which calls are already processed
    state_mgr = None
    if output_dir and company_name:
        state_mgr = StateManager(company_name, output_dir)

    # --- Fetch deck PDF if URL provided ---
    deck_path = None
    temp_dirs: list[Path] = []

    if deck_url:
        try:
            from src.ingestion.deck_fetcher import fetch_deck, detect_url_type

            url_type = detect_url_type(deck_url)
            type_label = {"google_drive": "Google Drive", "docsend": "DocSend"}.get(url_type, "URL")
            _post_blocks(slack_client, channel_id, thread_ts, format_deck_progress(type_label))

            dest_dir = Path(tempfile.mkdtemp(prefix="memo_deck_"))
            temp_dirs.append(dest_dir)
            deck_path = fetch_deck(deck_url, dest_dir)

            if deck_path:
                logger.info("Deck fetched: %s (%d KB)", deck_path, deck_path.stat().st_size // 1024)
            else:
                _post_blocks(
                    slack_client, channel_id, thread_ts,
                    [{"type": "context", "elements": [
                        {"type": "mrkdwn", "text": ":warning: Could not fetch deck. Continuing without it."},
                    ]}],
                )
        except Exception as e:
            logger.warning("Deck fetch failed: %s", e)
            _post_blocks(
                slack_client, channel_id, thread_ts,
                [{"type": "context", "elements": [
                    {"type": "mrkdwn", "text": f":warning: Deck fetch failed: {e}. Continuing without it."},
                ]}],
            )

    # --- Download unprocessed documents ---
    doc_paths: list[str] = []
    doc_names_downloaded: list[str] = []

    if doc_sources and state_mgr:
        unprocessed = [ds for ds in doc_sources if not state_mgr.is_document_processed(ds["name"])]
        if unprocessed:
            try:
                from src.ingestion.deck_fetcher import fetch_multiple_docs

                docs_dir = Path(tempfile.mkdtemp(prefix="memo_docs_"))
                temp_dirs.append(docs_dir)
                fetch_results = fetch_multiple_docs(unprocessed, docs_dir)

                for fr in fetch_results:
                    if fr["success"] and fr["path"]:
                        doc_paths.append(str(fr["path"]))
                        doc_names_downloaded.append(fr["name"])

                if doc_paths:
                    _post_blocks(
                        slack_client, channel_id, thread_ts,
                        [{"type": "context", "elements": [
                            {"type": "mrkdwn", "text": f":page_facing_up: Downloaded {len(doc_paths)} document(s) for processing"},
                        ]}],
                    )
            except Exception as e:
                logger.warning("Document download failed: %s", e)

    result = None
    processed_count = 0
    first_run = True

    for idx, note in enumerate(ordered, 1):
        text = note.get("content_plaintext", "")
        title = note.get("title", f"Transcript {idx}")

        # Detect call stage for this transcript
        detected_stage = call_stage or detect_call_theme(text, client=anthropic_client)
        theme_name = THEME_NAMES.get(detected_stage, f"Call {detected_stage}")

        # Skip if already processed
        if state_mgr and state_mgr.has_processed_call(detected_stage):
            _post_blocks(slack_client, channel_id, thread_ts, format_call_skipped(detected_stage))
            continue

        # Post progress
        _post_blocks(
            slack_client, channel_id, thread_ts,
            format_multi_call_progress(idx, total, theme_name),
        )

        # Pass deck + docs only on the first pipeline run (they don't change between calls)
        documents = None
        if first_run:
            all_docs = []
            if deck_path:
                all_docs.append(str(deck_path))
            all_docs.extend(doc_paths)
            if all_docs:
                documents = all_docs

        # Run pipeline (state accumulates via use_state=True)
        result = run_pipeline(
            text,
            call_stage=detected_stage,
            output_dir=output_dir,
            skip_evals=skip_evals,
            client=anthropic_client,
            company_name=company_name,
            use_state=True,
            documents=documents,
        )
        processed_count += 1

        # Post enrichment stats if we used docs on this run
        if first_run and documents and result:
            enrichment = result.get("extraction", {}).get("_enrichment_stats")
            if enrichment:
                _post_blocks(slack_client, channel_id, thread_ts, format_deck_enriched(enrichment))

        first_run = False

        # Mark downloaded documents as processed after successful first run
        if processed_count == 1 and doc_names_downloaded and state_mgr:
            for doc_name in doc_names_downloaded:
                source = "drive"
                for ds in (doc_sources or []):
                    if ds["name"] == doc_name:
                        source = ds.get("source", "drive")
                        break
                state_mgr.add_processed_document(doc_name, source=source)

        # Reload state manager so next iteration sees updated calls_processed
        if state_mgr:
            state_mgr = StateManager(company_name, output_dir)

    if result is None:
        # All transcripts were already processed — load latest memo from state
        if state_mgr:
            state = state_mgr.state
            latest_memo = state_mgr.get_latest_memo() or ""
            result = {
                "extraction": {},
                "gap_analysis": {},
                "memo": latest_memo,
                "contradictions": [],
            }
            _post_blocks(
                slack_client, channel_id, thread_ts,
                [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": ":white_check_mark: All transcripts already processed. Showing latest memo."},
                }],
            )
        else:
            result = {"extraction": {}, "gap_analysis": {}, "memo": "", "contradictions": []}

    # --- Write extracted data back to Attio ---
    if deal and result.get("extraction"):
        _write_back_to_attio(
            slack_client, channel_id, thread_ts,
            extraction=result["extraction"],
            deal=deal,
            calls_processed=state_mgr.state.get("calls_processed", []) if state_mgr else [],
        )

    # Clean up temp directories (deck + docs)
    for temp_dir in temp_dirs:
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except OSError:
            pass

    return result


# --- Extraction → Attio field mapping ---

# Maps extraction JSON paths to Attio deal field slugs
EXTRACTION_TO_ATTIO = {
    "company.industry": "sector",
    "round_dynamics.raising_amount": "target_raise",
    "round_dynamics.valuation": "initial_round_valuation_cap",
    "round_dynamics.instrument": "funding_round",
}

# Deal stage labels based on calls processed
_DEAL_STAGE_LABELS = {
    1: "Call 1 Complete",
    2: "Call 2 Complete",
    3: "Call 3 Complete",
}


def _extract_nested(data: dict, dotted_key: str):
    """Extract a value from a nested dict using dot notation (e.g. 'company.industry')."""
    parts = dotted_key.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    # Handle attributed values ({value, source} dicts)
    if isinstance(current, dict) and "value" in current:
        current = current["value"]
    return current


def _is_valid_value(val) -> bool:
    """Check if a value is non-empty and not a TBD placeholder."""
    if val is None:
        return False
    if isinstance(val, str):
        s = val.strip().lower()
        return bool(s) and not s.startswith("[tbd") and s != "tbd"
    return True


def _write_back_to_attio(
    slack_client,
    channel_id: str,
    thread_ts: str,
    *,
    extraction: dict,
    deal: dict,
    calls_processed: list[int],
):
    """Map extracted fields to Attio deal fields and write back."""
    entry_id = deal.get("entry_id")
    if not entry_id:
        logger.warning("No entry_id in deal data, skipping Attio write-back")
        return

    updates = {}
    written_fields = []

    for ext_path, attio_field in EXTRACTION_TO_ATTIO.items():
        val = _extract_nested(extraction, ext_path)
        existing = deal.get(attio_field)

        # Only write if extracted value is valid and Attio field is empty
        if _is_valid_value(val) and not _is_valid_value(existing):
            updates[attio_field] = val
            written_fields.append(attio_field.replace("_", " ").title())

    # Update deal stage based on highest call processed
    if calls_processed:
        max_call = max(calls_processed)
        stage_label = _DEAL_STAGE_LABELS.get(max_call)
        if stage_label:
            updates["deal_stage"] = stage_label
            written_fields.append("Deal Stage")

    if not updates:
        return

    try:
        attio = AttioClient()
        attio.update_deal_entry(entry_id, updates)
        _post_blocks(slack_client, channel_id, thread_ts, format_attio_writeback(written_fields))
    except Exception as e:
        logger.warning("Attio write-back failed: %s", e)
        _post_blocks(
            slack_client, channel_id, thread_ts,
            [{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":warning: Attio write-back failed: {e}"},
            ]}],
        )


def _post_pipeline_results(
    client, channel_id: str, thread_ts: str, result: dict, company_name: str | None,
    *, deal_folder: dict | None = None,
):
    """Post the final pipeline results to the Slack thread."""
    # 1. Post extraction summary
    extraction = result.get("extraction", {})
    if extraction:
        _post_blocks(client, channel_id, thread_ts, format_extraction_summary(extraction))

    # 2. Post gap analysis
    gap_analysis = result.get("gap_analysis", {})
    if gap_analysis:
        _post_blocks(client, channel_id, thread_ts, format_gap_analysis(gap_analysis))

    # 3. Post memo
    memo = result.get("memo", "")
    if memo:
        memo_formatted = format_memo(memo, company_name)

        if "blocks" in memo_formatted:
            _post_blocks(client, channel_id, thread_ts, memo_formatted["blocks"])
        elif "file" in memo_formatted:
            file_info = memo_formatted["file"]
            client.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                content=file_info["content"],
                filename=file_info["filename"],
                title=file_info["title"],
                initial_comment=file_info["initial_comment"],
            )

    # 4. Google Docs export (place in deal folder if available)
    _export_google_doc(client, channel_id, thread_ts, memo, company_name, deal_folder=deal_folder)

    # 5. Post eval report (if available)
    eval_report = result.get("eval_report")
    if eval_report:
        _post_blocks(client, channel_id, thread_ts, format_eval_report(eval_report))

    # 6. Post completion summary
    _post_blocks(client, channel_id, thread_ts, format_pipeline_complete(result))


def _export_google_doc(
    client, channel_id: str, thread_ts: str, memo: str, company_name: str | None,
    *, deal_folder: dict | None = None,
):
    """Export memo to Google Docs if configured. Fails gracefully.

    Places the doc inside the deal folder when available.
    """
    try:
        from src.integrations.google_docs import GoogleDocsClient, is_configured as gdocs_configured

        if not gdocs_configured():
            return

        gdocs = GoogleDocsClient()
        folder_id = deal_folder["folder_id"] if deal_folder else None
        doc_info = gdocs.create_memo_doc(memo, company_name, folder_id=folder_id)
        _post_blocks(
            client, channel_id, thread_ts,
            format_google_doc_link(doc_info["doc_url"], doc_info["title"]),
        )
    except Exception as e:
        logger.warning("Google Docs export failed: %s", e)
        _post_blocks(
            client, channel_id, thread_ts,
            [{"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":warning: Google Docs export failed: {e}"},
            ]}],
        )


def _post_blocks(client, channel: str, thread_ts: str, blocks: list[dict]):
    """Post blocks to a thread, with fallback text."""
    # Extract text from first section block for fallback
    fallback = "Pipeline update"
    for b in blocks:
        text_obj = b.get("text", {})
        if isinstance(text_obj, dict) and "text" in text_obj:
            fallback = text_obj["text"][:200]
            break

    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=blocks,
        text=fallback,
    )


# --- Main ---


def main():
    """Start the bot in Socket Mode for local development."""
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print(
            "Error: SLACK_APP_TOKEN not set. Socket Mode requires an app-level token.\n"
            "Set it in .env or export SLACK_APP_TOKEN=xapp-..."
        )
        raise SystemExit(1)

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        print(
            "Error: SLACK_BOT_TOKEN not set.\n"
            "Set it in .env or export SLACK_BOT_TOKEN=xoxb-..."
        )
        raise SystemExit(1)

    bolt_app = create_app()

    print("Starting memo-agent Slack bot (Socket Mode)...")
    print("  Listening for /memo commands")
    print("  Press Ctrl+C to stop")

    handler = SocketModeHandler(bolt_app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
