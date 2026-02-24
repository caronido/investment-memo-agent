from __future__ import annotations

"""Command parsing for the /memo slash command.

Parses the slash command text to extract company name, sub-commands,
and options. Supports fuzzy matching against existing company directories.

Commands:
    /memo                        — opens transcript paste modal
    /memo lazo.us                — search Attio by domain, pull transcripts
    /memo lazo.us status         — show current memo state
    /memo lazo.us questions      — show missing questions
    /memo lazo.us --call-stage 2 — specify call stage
"""

from pathlib import Path

# Default output directory for pipeline state
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "output"

# Minimum transcript length (chars) to be considered valid
MIN_TRANSCRIPT_LENGTH = 200

# Recognized sub-commands
SUBCOMMANDS = {"status", "questions"}


def parse_memo_command(text: str) -> dict:
    """Parse /memo slash command text.

    Args:
        text: The raw text after /memo.

    Returns:
        Dict with keys:
            company_name: str | None
            call_stage: int | None
            subcommand: str | None — "status" or "questions"
    """
    text = text.strip()
    if not text:
        return {"company_name": None, "call_stage": None, "subcommand": None}

    parts = text.split()
    company_name = None
    call_stage = None
    subcommand = None

    i = 0
    name_parts = []
    while i < len(parts):
        token = parts[i]

        # Flag: --call-stage N / --stage N / -s N
        if token in ("--call-stage", "--stage", "-s") and i + 1 < len(parts):
            try:
                call_stage = int(parts[i + 1])
            except ValueError:
                pass
            i += 2
            continue

        # Sub-command check (only if it's the last token or after company name)
        if token.lower() in SUBCOMMANDS:
            subcommand = token.lower()
            i += 1
            continue

        name_parts.append(token)
        i += 1

    if name_parts:
        company_name = " ".join(name_parts)

    return {
        "company_name": company_name,
        "call_stage": call_stage,
        "subcommand": subcommand,
    }


def parse_initial_evaluation_command(text: str) -> dict:
    """Parse /initial-evaluation slash command text.

    Args:
        text: The raw text after /initial-evaluation.

    Returns:
        Dict with key: company_name (str | None).
    """
    text = text.strip()
    if not text:
        return {"company_name": None}
    return {"company_name": text}


def find_company_dir(company_name: str, output_dir: Path | None = None) -> Path | None:
    """Find an existing company output directory by fuzzy name match.

    Args:
        company_name: Company name to search for.
        output_dir: Directory containing company subdirectories.

    Returns:
        Path to the company directory, or None if not found.
    """
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR

    if not output_dir.exists():
        return None

    target = company_name.lower().strip().replace(" ", "_")

    # Exact match first
    for d in output_dir.iterdir():
        if d.is_dir() and d.name.lower() == target:
            return d

    # Substring match
    for d in output_dir.iterdir():
        if d.is_dir() and (target in d.name.lower() or d.name.lower() in target):
            return d

    return None


def get_output_dir(company_name: str, base_dir: Path | None = None) -> Path:
    """Get or create the output directory for a company.

    Args:
        company_name: Company name (used as directory name).
        base_dir: Base output directory.

    Returns:
        Path to the company output directory.
    """
    if base_dir is None:
        base_dir = DEFAULT_OUTPUT_DIR

    # Normalize company name for directory
    dir_name = company_name.lower().strip().replace(" ", "_")
    company_dir = base_dir / dir_name
    company_dir.mkdir(parents=True, exist_ok=True)
    return company_dir


def validate_transcript(transcript: str) -> str | None:
    """Validate a transcript is long enough to process.

    Returns:
        Error message string, or None if valid.
    """
    if not transcript or not transcript.strip():
        return "Transcript is empty. Please paste a call transcript."

    if len(transcript.strip()) < MIN_TRANSCRIPT_LENGTH:
        return (
            f"Transcript is too short ({len(transcript.strip())} chars). "
            f"Minimum is {MIN_TRANSCRIPT_LENGTH} chars. "
            "Please paste the full call transcript."
        )

    return None
