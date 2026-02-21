from __future__ import annotations

"""PDF document extraction via Claude's vision API.

Sends PDF pages as images to Claude, extracting structured data in the same
schema format as transcript extraction. Supports pitch decks, financial models,
and other investment-related documents.

CLI:
    python -m src.ingestion.document_processor --pdf data/documents/deck.pdf --call-stage 1 --output data/output/deck_extraction.json
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-20250514"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

SCHEMA_FILES = {
    1: "extraction_call1.json",
    2: "extraction_call2.json",
    3: "extraction_call3.json",
    4: "extraction_call4.json",
}

# Max pages to send in a single request (Claude vision limit / cost control)
MAX_PAGES = 30

DOCUMENT_EXTRACTION_PROMPT = """\
Extract structured data from this pitch deck / investment document and return it as JSON.

RULES:
1. Only extract information explicitly shown in the document. Use null for fields \
where no data is available. Do NOT hallucinate.
2. The document may be in Spanish, English, or a mix of both. Output all extracted \
data in English.
3. In the "sources" array, include the page number and a brief description of what \
was found, with source_type set to "deck".
4. For each source entry, use the format: {{"field": "field.name", "source_type": "deck", \
"page": <page_number>, "quote": "<brief description of what the slide shows>"}}
5. Return ONLY valid JSON matching the schema. No markdown fences, no extra text.

EXTRACTION FOCUS:
- Company name, one-liner, industry, geography, stage
- Founder backgrounds and team composition
- Round dynamics (raising amount, valuation, instrument, use of funds)
- Business model (revenue model, pricing, target customer)
- Traction (ARR/MRR, customers, growth rate, key metrics)
- Market sizing (TAM, SAM, SOM)
- Product description, key features, technology stack
- Competitive landscape
- Financial projections and unit economics

SCHEMA:
{schema}

Return a single JSON object matching this schema."""


def _load_schema(call_stage: int) -> str:
    """Load the JSON schema for a given call stage."""
    schema_path = SCHEMAS_DIR / SCHEMA_FILES[call_stage]
    return schema_path.read_text()


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, stripping markdown fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        start = 1 if lines[0].startswith("```") else 0
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        cleaned = "\n".join(lines[start:end])
    return json.loads(cleaned)


def _pdf_to_base64_pages(pdf_path: Path) -> list[str]:
    """Convert a PDF file to a list of base64-encoded page images.

    Uses pdf2image if available. Returns empty list if pdf2image is missing
    or if rendered pages are blank (some PDFs use embedded fonts/layers that
    don't rasterize).
    """
    try:
        import io
        from pdf2image import convert_from_path
        images = convert_from_path(str(pdf_path), dpi=150, fmt="png")
        pages = []
        for img in images[:MAX_PAGES]:
            # Resize if wider than 1600px to keep request size manageable
            if img.width > 1600:
                ratio = 1600 / img.width
                img = img.resize((1600, int(img.height * ratio)))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            page_bytes = buf.getvalue()
            # Skip blank pages (< 10KB usually means empty/white)
            if len(page_bytes) < 10_000:
                continue
            pages.append(base64.standard_b64encode(page_bytes).decode("utf-8"))
        return pages
    except ImportError:
        return []


# Max PDF size for native document mode (32MB base64 ≈ 24MB raw)
_MAX_PDF_BYTES = 24 * 1024 * 1024


def _trim_pdf(pdf_path: Path, max_pages: int = MAX_PAGES) -> bytes:
    """Read a PDF file and trim to max_pages if needed.

    Uses pypdf if available to reduce page count for large PDFs.
    Falls back to reading the raw file.
    """
    raw_bytes = pdf_path.read_bytes()

    if len(raw_bytes) <= _MAX_PDF_BYTES:
        return raw_bytes

    # Try trimming pages with pypdf
    try:
        from pypdf import PdfReader, PdfWriter
        import io

        reader = PdfReader(io.BytesIO(raw_bytes))
        if len(reader.pages) <= max_pages:
            return raw_bytes

        writer = PdfWriter()
        for page in reader.pages[:max_pages]:
            writer.add_page(page)

        buf = io.BytesIO()
        writer.write(buf)
        trimmed = buf.getvalue()

        if len(trimmed) <= _MAX_PDF_BYTES:
            return trimmed
    except ImportError:
        pass

    return raw_bytes


def _build_vision_content(
    pdf_path: Path,
    call_stage: int,
) -> list[dict]:
    """Build the content blocks for Claude's vision API.

    Strategy:
    1. Try pdf2image (page-as-JPEG). Best quality but requires poppler.
    2. If images are blank or pdf2image unavailable, use Claude's native PDF
       document support (base64 PDF).
    """
    schema_text = _load_schema(call_stage)
    prompt_text = DOCUMENT_EXTRACTION_PROMPT.replace("{schema}", schema_text)

    page_images = _pdf_to_base64_pages(pdf_path)

    content = []

    if page_images:
        # Send each page as an image
        for i, img_b64 in enumerate(page_images):
            content.append({
                "type": "text",
                "text": f"--- Page {i + 1} ---",
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": img_b64,
                },
            })
    else:
        # Native PDF document mode
        pdf_bytes = _trim_pdf(pdf_path)
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            },
        })

    content.append({
        "type": "text",
        "text": prompt_text,
    })

    return content


def extract_from_document(
    pdf_path: str | Path,
    call_stage: int = 1,
    *,
    client: anthropic.Anthropic | None = None,
) -> dict:
    """Extract structured data from a PDF document using Claude's vision API.

    Args:
        pdf_path: Path to the PDF file.
        call_stage: Call stage schema to use (1-4). Defaults to 1 since decks
            typically contain founder story / overview data.
        client: Optional shared Anthropic client.

    Returns:
        Extraction dict matching the schema for the given call_stage.
        Sources will have source_type="deck" with page numbers.

    Raises:
        FileNotFoundError: If the PDF file doesn't exist.
        ValueError: If call_stage is invalid.
        json.JSONDecodeError: If Claude returns unparseable JSON after retry.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if call_stage not in SCHEMA_FILES:
        raise ValueError(f"Invalid call_stage: {call_stage}. Must be 1, 2, 3, or 4.")

    if client is None:
        client = anthropic.Anthropic()

    content = _build_vision_content(pdf_path, call_stage)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )
    response_text = response.content[0].text

    try:
        data = _parse_json_response(response_text)
    except json.JSONDecodeError:
        # Retry once
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": response_text},
                {
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. Please return ONLY "
                        "a valid JSON object matching the schema, with no "
                        "additional text or markdown fences."
                    ),
                },
            ],
        )
        data = _parse_json_response(retry_response.content[0].text)

    # Ensure call_stage and metadata
    data["call_stage"] = call_stage
    data["_source_document"] = pdf_path.name

    # Ensure all sources have source_type="deck"
    sources = data.get("sources", [])
    for src in sources:
        if isinstance(src, dict) and src.get("source_type") != "deck":
            src["source_type"] = "deck"
    data["sources"] = sources

    return data


def main():
    parser = argparse.ArgumentParser(
        description="Extract structured data from a PDF document using Claude vision."
    )
    parser.add_argument(
        "--pdf", required=True, help="Path to PDF file"
    )
    parser.add_argument(
        "--call-stage",
        type=int,
        choices=[1, 2, 3, 4],
        default=1,
        help="Call stage schema to use (default: 1).",
    )
    parser.add_argument(
        "--output", required=True, help="Path to write output JSON"
    )

    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing PDF: {pdf_path} ({pdf_path.stat().st_size / 1024:.1f} KB)")
    print(f"Using call stage {args.call_stage} schema")

    result = extract_from_document(pdf_path, args.call_stage)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"\nExtraction saved to {output_path}")
    print(f"Top-level keys: {list(result.keys())}")
    sources = result.get("sources", [])
    print(f"Sources: {len(sources)} entries")


if __name__ == "__main__":
    main()
