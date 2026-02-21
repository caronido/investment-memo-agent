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
    format_call_skipped,
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
                    blocks = format_attio_company(company)
                    n = len(transcripts)
                    blocks.append({
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f":rocket: Processing {n} transcript(s) chronologically..."},
                    })
                    ack_msg = client.chat_postMessage(
                        channel=channel_id,
                        blocks=blocks,
                        text=f"Found {resolved_name} in Attio. Processing {n} transcript(s)...",
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
        _post_pipeline_results(client, channel_id, thread_ts, result, company_name)

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
) -> dict:
    """Process multiple transcripts chronologically, skipping already-processed calls.

    Returns the result dict from the last pipeline run (the most complete memo).
    """
    # Reverse to oldest-first (Attio returns newest-first)
    ordered = list(reversed(transcripts))
    total = len(ordered)

    # Load existing state to check which calls are already processed
    state_mgr = None
    if output_dir and company_name:
        state_mgr = StateManager(company_name, output_dir)

    result = None
    processed_count = 0

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

        # Run pipeline (state accumulates via use_state=True)
        result = run_pipeline(
            text,
            call_stage=detected_stage,
            output_dir=output_dir,
            skip_evals=skip_evals,
            client=anthropic_client,
            company_name=company_name,
            use_state=True,
        )
        processed_count += 1

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

    return result


def _post_pipeline_results(
    client, channel_id: str, thread_ts: str, result: dict, company_name: str | None,
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

    # 4. Google Docs export
    _export_google_doc(client, channel_id, thread_ts, memo, company_name)

    # 5. Post eval report (if available)
    eval_report = result.get("eval_report")
    if eval_report:
        _post_blocks(client, channel_id, thread_ts, format_eval_report(eval_report))

    # 6. Post completion summary
    _post_blocks(client, channel_id, thread_ts, format_pipeline_complete(result))


def _export_google_doc(
    client, channel_id: str, thread_ts: str, memo: str, company_name: str | None,
):
    """Export memo to Google Docs if configured. Fails gracefully."""
    try:
        from src.integrations.google_docs import GoogleDocsClient, is_configured as gdocs_configured

        if not gdocs_configured():
            return

        gdocs = GoogleDocsClient()
        doc_info = gdocs.create_memo_doc(memo, company_name)
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
