from __future__ import annotations

"""Deck PDF fetcher — downloads pitch decks from Google Drive, DocSend, or direct URLs.

Routes by URL type:
- Google Drive → download via Drive API (reuses service account credentials)
- DocSend → convert via docsend2pdf.com/api/convert
- Direct URL → download with httpx

Usage:
    from src.ingestion.deck_fetcher import fetch_deck
    path = fetch_deck("https://drive.google.com/file/d/abc123/view", Path("/tmp/decks"))
"""

import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Timeout for HTTP downloads (decks can be large)
_DOWNLOAD_TIMEOUT = 120.0


def fetch_deck(url: str, dest_dir: Path) -> Path | None:
    """Download a deck PDF from a URL. Routes by URL type.

    Args:
        url: URL to the deck (Google Drive, DocSend, or direct link).
        dest_dir: Directory to save the downloaded PDF.

    Returns:
        Path to the downloaded PDF, or None on failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Google Drive
        file_id = _parse_drive_file_id(url)
        if file_id:
            dest_path = dest_dir / f"deck_{file_id[:12]}.pdf"
            return _fetch_from_drive(file_id, dest_path)

        # DocSend
        if "docsend.com" in url:
            dest_path = dest_dir / "deck_docsend.pdf"
            return _fetch_from_docsend(url, dest_path)

        # Direct URL (assume PDF)
        dest_path = dest_dir / "deck_direct.pdf"
        return _fetch_direct(url, dest_path)

    except Exception as e:
        logger.error("Failed to fetch deck from %s: %s", url, e)
        return None


def _parse_drive_file_id(url: str) -> str | None:
    """Extract file ID from Google Drive URLs.

    Supports:
    - drive.google.com/file/d/{id}/...
    - drive.google.com/open?id={id}
    - docs.google.com/presentation/d/{id}/...
    """
    patterns = [
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
        r"docs\.google\.com/(?:presentation|document|spreadsheets)/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _fetch_from_drive(file_id: str, dest_path: Path) -> Path:
    """Download a file from Google Drive via the service account.

    Reuses the GoogleDocsClient credentials infrastructure.
    """
    from src.integrations.google_docs import GoogleDocsClient, is_configured

    if not is_configured():
        raise RuntimeError(
            "Google service account not configured. "
            "Set GOOGLE_SERVICE_ACCOUNT_KEY_PATH or GOOGLE_SERVICE_ACCOUNT_KEY_JSON."
        )

    gdocs = GoogleDocsClient()
    return gdocs.download_file(file_id, dest_path)


def _fetch_from_docsend(url: str, dest_path: Path) -> Path:
    """Convert a DocSend link to PDF via docsend2pdf.com."""
    logger.info("Converting DocSend link via docsend2pdf.com: %s", url)

    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT) as client:
        response = client.post(
            "https://docsend2pdf.com/api/convert",
            json={"url": url},
        )
        response.raise_for_status()

        # The API returns the PDF content directly
        if response.headers.get("content-type", "").startswith("application/pdf"):
            dest_path.write_bytes(response.content)
        else:
            # Some implementations return a download URL
            data = response.json()
            download_url = data.get("url") or data.get("download_url")
            if not download_url:
                raise ValueError(
                    f"docsend2pdf returned unexpected response: {list(data.keys())}"
                )
            pdf_response = client.get(download_url)
            pdf_response.raise_for_status()
            dest_path.write_bytes(pdf_response.content)

    logger.info("DocSend PDF saved to %s (%d KB)", dest_path, dest_path.stat().st_size // 1024)
    return dest_path


def _fetch_direct(url: str, dest_path: Path) -> Path:
    """Download a URL directly with httpx."""
    logger.info("Downloading deck directly: %s", url)

    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        dest_path.write_bytes(response.content)

    logger.info("Deck saved to %s (%d KB)", dest_path, dest_path.stat().st_size // 1024)
    return dest_path


def fetch_document(source: dict, dest_dir: Path) -> Path | None:
    """Download a single document from a URL or Drive file metadata.

    Args:
        source: Dict with either:
            - {"url": str} — a URL to download (Drive, DocSend, or direct)
            - {"file_id": str, "name": str} — a Drive file to download directly
        dest_dir: Directory to save the downloaded file.

    Returns:
        Path to the downloaded file, or None on failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Drive file metadata (from list_folder_files)
        file_id = source.get("file_id")
        if file_id and not source.get("url"):
            name = source.get("name", f"doc_{file_id[:12]}.pdf")
            dest_path = dest_dir / name
            mime = source.get("mime_type", "")

            from src.integrations.google_docs import GoogleDocsClient, is_configured

            if not is_configured():
                logger.warning("Google not configured, cannot download file %s", file_id)
                return None

            gdocs = GoogleDocsClient()

            # Google Workspace files need export; native files use direct download
            export_mimes = {
                "application/vnd.google-apps.presentation": "application/pdf",
                "application/vnd.google-apps.document": "application/pdf",
                "application/vnd.google-apps.spreadsheet": "application/pdf",
            }
            if mime in export_mimes:
                import io
                from googleapiclient.http import MediaIoBaseDownload

                request = gdocs._drive_service.files().export_media(
                    fileId=file_id,
                    mimeType=export_mimes[mime],
                )
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                pdf_path = dest_path.with_suffix(".pdf")
                pdf_path.write_bytes(fh.getvalue())
                logger.info("Exported %s to %s (%d KB)", name, pdf_path, len(fh.getvalue()) // 1024)
                return pdf_path
            else:
                return gdocs.download_file(file_id, dest_path)

        # URL-based download
        url = source.get("url")
        if url:
            return fetch_deck(url, dest_dir)

        logger.warning("Document source has neither url nor file_id: %s", source)
        return None

    except Exception as e:
        logger.error("Failed to fetch document %s: %s", source.get("name") or source.get("url", "?"), e)
        return None


def fetch_multiple_docs(sources: list[dict], dest_dir: Path) -> list[dict]:
    """Download multiple documents from a mix of URLs and Drive file metadata.

    Args:
        sources: List of source dicts, each with 'url' or 'file_id'+'name'.
        dest_dir: Directory to save downloaded files.

    Returns:
        List of dicts with: name, path (Path or None), source, success (bool).
    """
    results = []
    for source in sources:
        name = source.get("name") or source.get("url", "unknown")
        path = fetch_document(source, dest_dir)
        results.append({
            "name": name,
            "path": path,
            "source": source.get("source", "unknown"),
            "success": path is not None,
        })
        if path:
            logger.info("Downloaded: %s → %s", name, path)
        else:
            logger.warning("Failed to download: %s", name)

    succeeded = sum(1 for r in results if r["success"])
    logger.info("Fetched %d/%d documents", succeeded, len(sources))
    return results


def detect_url_type(url: str) -> str:
    """Classify a deck URL by source type.

    Returns:
        "google_drive", "docsend", or "direct".
    """
    if _parse_drive_file_id(url):
        return "google_drive"
    if "docsend.com" in url:
        return "docsend"
    return "direct"
