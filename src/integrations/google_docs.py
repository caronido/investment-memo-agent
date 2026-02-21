from __future__ import annotations

"""Google Docs export for investment memos.

Creates formatted Google Docs from markdown memo text using a service account.
The doc is placed in a shared Google Drive folder.

Requires:
    GOOGLE_SERVICE_ACCOUNT_KEY_PATH — path to service account JSON key file
    GOOGLE_DRIVE_FOLDER_ID — shared Google Drive folder for memos

Setup:
    1. Enable Google Docs API and Google Drive API in Google Cloud Console
    2. Create a Service Account and download the JSON key file
    3. Share the target Drive folder with the service account email (as Editor)
    4. Set the env vars above
"""

import logging
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """Check if Google Docs export is configured."""
    return bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")
        and os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    )


class GoogleDocsClient:
    """Client for creating formatted Google Docs from markdown memos."""

    SCOPES = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive.file",
    ]

    def __init__(
        self,
        key_path: str | None = None,
        folder_id: str | None = None,
    ):
        self.key_path = key_path or os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")
        self.folder_id = folder_id or os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

        if not self.key_path:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_KEY_PATH not set")
        if not self.folder_id:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID not set")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            self.key_path, scopes=self.SCOPES
        )
        self._docs_service = build("docs", "v1", credentials=credentials)
        self._drive_service = build("drive", "v3", credentials=credentials)

    def create_memo_doc(self, memo: str, company_name: str | None = None) -> dict:
        """Create a Google Doc from a markdown memo.

        Args:
            memo: Markdown-formatted investment memo.
            company_name: Optional company name for the document title.

        Returns:
            Dict with: doc_id, doc_url, title.
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = f"Investment Memo — {company_name or 'Draft'} ({date_str})"

        # Create empty doc in the shared folder via Drive API
        file_metadata = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [self.folder_id],
        }
        file = self._drive_service.files().create(
            body=file_metadata,
            fields="id,webViewLink",
            supportsAllDrives=True,
        ).execute()

        doc_id = file["id"]
        doc_url = file.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}/edit")

        # Convert markdown to Docs API requests and apply
        requests, end_index = self._markdown_to_docs_requests(memo)
        if requests:
            # Apply Manrope font to the entire document
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": 1, "endIndex": end_index},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Manrope"}},
                    "fields": "weightedFontFamily",
                }
            })
            self._docs_service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()

        logger.info("Created Google Doc: %s (%s)", title, doc_url)
        return {"doc_id": doc_id, "doc_url": doc_url, "title": title}

    def _markdown_to_docs_requests(self, markdown: str) -> tuple[list[dict], int]:
        """Convert markdown text to Google Docs API batchUpdate requests.

        Handles: # H1, ## H2, ### H3, **bold**, - bullet points, paragraphs.
        Uses cursor-based index tracking for sequential inserts.

        Returns:
            Tuple of (requests list, final cursor index).
        """
        lines = markdown.split("\n")
        requests: list[dict] = []
        # Google Docs starts at index 1 (after the implicit newline)
        cursor = 1

        for line in lines:
            stripped = line.strip()

            if not stripped:
                # Empty line — insert a newline for paragraph spacing
                requests.append({
                    "insertText": {"location": {"index": cursor}, "text": "\n"}
                })
                cursor += 1
                continue

            # Detect heading level
            heading_style = None
            if stripped.startswith("### "):
                heading_style = "HEADING_3"
                stripped = stripped[4:]
            elif stripped.startswith("## "):
                heading_style = "HEADING_2"
                stripped = stripped[3:]
            elif stripped.startswith("# "):
                heading_style = "HEADING_1"
                stripped = stripped[2:]

            # Detect bullet
            is_bullet = False
            if stripped.startswith("- ") or stripped.startswith("* "):
                is_bullet = True
                stripped = stripped[2:]

            # Process inline bold formatting
            clean_text, bold_ranges = self._process_inline_formatting(stripped)
            insert_text = clean_text + "\n"

            # Insert the text
            requests.append({
                "insertText": {"location": {"index": cursor}, "text": insert_text}
            })

            # Apply heading style
            if heading_style:
                requests.append({
                    "updateParagraphStyle": {
                        "range": {"startIndex": cursor, "endIndex": cursor + len(insert_text)},
                        "paragraphStyle": {"namedStyleType": heading_style},
                        "fields": "namedStyleType",
                    }
                })

            # Apply bullet style
            if is_bullet:
                requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": cursor, "endIndex": cursor + len(insert_text)},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                })

            # Apply bold formatting
            for start, end in bold_ranges:
                requests.append({
                    "updateTextStyle": {
                        "range": {
                            "startIndex": cursor + start,
                            "endIndex": cursor + end,
                        },
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                })

            cursor += len(insert_text)

        return requests, cursor

    @staticmethod
    def _process_inline_formatting(text: str) -> tuple[str, list[tuple[int, int]]]:
        """Strip **bold** markers and return clean text with bold ranges.

        Args:
            text: Text potentially containing **bold** markers.

        Returns:
            Tuple of (clean_text, list of (start, end) index ranges for bold).
        """
        bold_ranges: list[tuple[int, int]] = []
        clean = ""
        i = 0
        chars = list(text)
        n = len(chars)

        while i < n:
            if i < n - 1 and chars[i] == "*" and chars[i + 1] == "*":
                # Found opening **
                bold_start = len(clean)
                i += 2
                # Find closing **
                while i < n - 1:
                    if chars[i] == "*" and chars[i + 1] == "*":
                        bold_ranges.append((bold_start, len(clean)))
                        i += 2
                        break
                    clean += chars[i]
                    i += 1
                else:
                    # No closing ** found, just add remaining
                    if i < n:
                        clean += chars[i]
                        i += 1
            else:
                clean += chars[i]
                i += 1

        return clean, bold_ranges
