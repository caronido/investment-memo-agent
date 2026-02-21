from __future__ import annotations

"""Google Docs export for investment memos.

Creates formatted Google Docs from markdown memo text using a service account.
The doc is placed in a shared Google Drive folder.

Credentials can be provided two ways:
    1. GOOGLE_SERVICE_ACCOUNT_KEY_PATH — path to JSON key file (local dev)
    2. GOOGLE_SERVICE_ACCOUNT_KEY_JSON — raw JSON string (production/Railway)

Also requires:
    GOOGLE_DRIVE_FOLDER_ID — shared Google Drive folder for memos

Setup:
    1. Enable Google Docs API and Google Drive API in Google Cloud Console
    2. Create a Service Account and download the JSON key file
    3. Share the target Drive folder with the service account email (as Editor)
    4. Set the env vars above
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """Check if Google Docs export is configured."""
    has_credentials = bool(
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_JSON")
    )
    return has_credentials and bool(os.environ.get("GOOGLE_DRIVE_FOLDER_ID"))


class GoogleDocsClient:
    """Client for creating formatted Google Docs from markdown memos."""

    SCOPES = [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(
        self,
        key_path: str | None = None,
        key_json: str | None = None,
        folder_id: str | None = None,
    ):
        self.folder_id = folder_id or os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
        if not self.folder_id:
            raise ValueError("GOOGLE_DRIVE_FOLDER_ID not set")

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        # Load credentials: JSON string (production) or file path (local dev)
        raw_json = key_json or os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_JSON")
        raw_path = key_path or os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")

        if raw_json:
            info = json.loads(raw_json)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=self.SCOPES
            )
        elif raw_path:
            # Resolve relative paths against the project root (where .env lives)
            path = Path(raw_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent.parent / raw_path
            credentials = service_account.Credentials.from_service_account_file(
                str(path), scopes=self.SCOPES
            )
        else:
            raise ValueError(
                "Set GOOGLE_SERVICE_ACCOUNT_KEY_JSON (production) "
                "or GOOGLE_SERVICE_ACCOUNT_KEY_PATH (local dev)"
            )

        self._docs_service = build("docs", "v1", credentials=credentials)
        self._drive_service = build("drive", "v3", credentials=credentials)

    def create_or_get_deal_folder(self, company_name: str) -> dict:
        """Find or create a subfolder for a deal inside GOOGLE_DRIVE_FOLDER_ID.

        Searches for existing folder named '{company_name}' first.
        Creates one if not found.

        Args:
            company_name: Company name to use as folder name.

        Returns:
            Dict with: folder_id, folder_url, created.
        """
        # Search for existing folder by name in the parent folder
        query = (
            f"name = '{company_name}' and "
            f"'{self.folder_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )
        response = self._drive_service.files().list(
            q=query,
            fields="files(id,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()

        files = response.get("files", [])
        if files:
            folder = files[0]
            folder_id = folder["id"]
            folder_url = folder.get(
                "webViewLink",
                f"https://drive.google.com/drive/folders/{folder_id}",
            )
            logger.info("Found existing deal folder for %s: %s", company_name, folder_id)
            return {"folder_id": folder_id, "folder_url": folder_url, "created": False}

        # Create new folder
        file_metadata = {
            "name": company_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self.folder_id],
        }
        folder = self._drive_service.files().create(
            body=file_metadata,
            fields="id,webViewLink",
            supportsAllDrives=True,
        ).execute()

        folder_id = folder["id"]
        folder_url = folder.get(
            "webViewLink",
            f"https://drive.google.com/drive/folders/{folder_id}",
        )
        logger.info("Created deal folder for %s: %s", company_name, folder_id)
        return {"folder_id": folder_id, "folder_url": folder_url, "created": True}

    def list_folder_files(self, folder_id: str) -> list[dict]:
        """List all files in a Drive folder.

        Filters to supported document types (PDF, Google Slides/Docs).

        Args:
            folder_id: Google Drive folder ID.

        Returns:
            List of dicts with: file_id, name, mime_type, size, created_time.
        """
        supported_types = (
            "application/pdf",
            "application/vnd.google-apps.presentation",
            "application/vnd.google-apps.document",
            "application/vnd.google-apps.spreadsheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        query = f"'{folder_id}' in parents and trashed = false"
        response = self._drive_service.files().list(
            q=query,
            fields="files(id,name,mimeType,size,createdTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
            orderBy="createdTime",
        ).execute()

        files = []
        for f in response.get("files", []):
            mime = f.get("mimeType", "")
            if mime in supported_types:
                files.append({
                    "file_id": f["id"],
                    "name": f.get("name", ""),
                    "mime_type": mime,
                    "size": int(f.get("size", 0)) if f.get("size") else 0,
                    "created_time": f.get("createdTime", ""),
                })

        logger.info("Listed %d supported files in folder %s", len(files), folder_id)
        return files

    def upload_file_to_folder(self, file_path: Path, folder_id: str) -> dict:
        """Upload a local file to a Drive folder.

        Args:
            file_path: Path to the local file.
            folder_id: Target Drive folder ID.

        Returns:
            Dict with: file_id, name, web_view_link.
        """
        from googleapiclient.http import MediaFileUpload

        file_metadata = {
            "name": file_path.name,
            "parents": [folder_id],
        }
        media = MediaFileUpload(str(file_path), resumable=True)
        uploaded = self._drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()

        logger.info("Uploaded %s to folder %s", file_path.name, folder_id)
        return {
            "file_id": uploaded["id"],
            "name": uploaded.get("name", file_path.name),
            "web_view_link": uploaded.get("webViewLink", ""),
        }

    def create_memo_doc(
        self,
        memo: str,
        company_name: str | None = None,
        folder_id: str | None = None,
    ) -> dict:
        """Create a Google Doc from a markdown memo.

        Args:
            memo: Markdown-formatted investment memo.
            company_name: Optional company name for the document title.
            folder_id: Optional Drive folder ID to place the doc in.
                Falls back to the root shared folder.

        Returns:
            Dict with: doc_id, doc_url, title.
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = f"Investment Memo — {company_name or 'Draft'} ({date_str})"

        # Create empty doc in the target folder (deal folder or root)
        parent = folder_id or self.folder_id
        file_metadata = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [parent],
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

    def download_file(self, file_id: str, dest_path: Path) -> Path:
        """Download a file from Google Drive by ID.

        Args:
            file_id: Google Drive file ID.
            dest_path: Local path to save the file.

        Returns:
            The dest_path after successful download.
        """
        from googleapiclient.http import MediaIoBaseDownload
        import io

        request = self._drive_service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True,
        )
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(fh.getvalue())
        logger.info(
            "Downloaded Drive file %s to %s (%d KB)",
            file_id, dest_path, len(fh.getvalue()) // 1024,
        )
        return dest_path

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
