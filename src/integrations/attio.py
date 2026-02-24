from __future__ import annotations

"""Attio CRM API client.

Searches for companies, fetches records, and retrieves notes/transcripts
from Attio. Used by the Slack bot to auto-pull data when /memo [company]
is invoked.

Requires ATTIO_API_KEY environment variable.

API docs: https://docs.attio.com/rest-api
"""

import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.attio.com/v2"


class AttioClient:
    """Client for the Attio CRM REST API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ATTIO_API_KEY")
        if not self.api_key:
            raise ValueError(
                "ATTIO_API_KEY not set. Provide it as argument or set in .env"
            )
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def search_companies(self, query: str, limit: int = 5) -> list[dict]:
        """Search for companies by name using fuzzy matching.

        Args:
            query: Company name or search term (max 256 chars).
            limit: Max results to return (default 5).

        Returns:
            List of company result dicts with keys:
            record_id, name, domains, object_slug, web_url.
        """
        response = self._client.post(
            "/objects/records/search",
            json={
                "query": query[:256],
                "objects": ["companies"],
                "limit": limit,
            },
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("data", []):
            record_id = item.get("id", {}).get("record_id")
            results.append({
                "record_id": record_id,
                "name": item.get("record_text", ""),
                "domains": item.get("domains", []),
                "object_slug": item.get("object_slug", "companies"),
                "image_url": item.get("record_image"),
            })

        return results

    def get_company(self, record_id: str) -> dict:
        """Fetch a full company record by ID.

        Args:
            record_id: UUID of the company record.

        Returns:
            Dict with company details: record_id, name, web_url, values.
        """
        response = self._client.get(
            f"/objects/companies/records/{record_id}",
        )
        response.raise_for_status()
        data = response.json().get("data", {})

        return {
            "record_id": data.get("id", {}).get("record_id"),
            "web_url": data.get("web_url"),
            "created_at": data.get("created_at"),
            "values": _flatten_values(data.get("values", {})),
        }

    def get_notes(
        self, record_id: str, limit: int = 50
    ) -> list[dict]:
        """Fetch notes attached to a company record.

        Notes often contain call transcripts or meeting summaries.

        Args:
            record_id: UUID of the parent record.
            limit: Max notes to return.

        Returns:
            List of note dicts with: note_id, title, content_plaintext,
            content_markdown, created_at.
        """
        response = self._client.get(
            "/notes",
            params={
                "parent_object": "companies",
                "parent_record_id": record_id,
                "limit": limit,
            },
        )
        response.raise_for_status()
        data = response.json()

        notes = []
        for item in data.get("data", []):
            notes.append({
                "note_id": item.get("id", {}).get("note_id"),
                "title": item.get("title", ""),
                "content_plaintext": item.get("content_plaintext", ""),
                "content_markdown": item.get("content_markdown", ""),
                "created_at": item.get("created_at"),
            })

        return notes

    def find_transcripts(self, record_id: str) -> list[dict]:
        """Find notes that look like call transcripts.

        Filters notes by length and content heuristics (speaker patterns,
        timestamps, etc.).

        Args:
            record_id: UUID of the company record.

        Returns:
            List of transcript-like notes, sorted by created_at desc.
        """
        notes = self.get_notes(record_id)

        transcripts = []
        for note in notes:
            text = note.get("content_plaintext", "")
            # Heuristic: transcripts are long and contain speaker patterns
            if len(text) < 500:
                continue
            # Look for speaker turn indicators
            has_speakers = any(
                indicator in text.lower()
                for indicator in [
                    "speaker ", ":", ">> ", "interviewer", "founder",
                    "renata", "maria", "roberto",  # Nido team members
                ]
            )
            if has_speakers or len(text) > 2000:
                transcripts.append(note)

        # Sort by created_at descending (most recent first)
        transcripts.sort(
            key=lambda n: n.get("created_at", ""), reverse=True
        )
        return transcripts

    def search_by_domain(self, domain: str) -> list[dict]:
        """Search for companies by domain using exact match.

        Args:
            domain: Domain to search for (e.g., "lazo.us").

        Returns:
            List of company result dicts.
        """
        response = self._client.post(
            "/objects/companies/records/query",
            json={
                "filter": {
                    "domains": {"domain": {"$eq": domain}},
                },
                "limit": 5,
            },
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("data", []):
            record_id = item.get("id", {}).get("record_id")
            # Extract name from values
            name_values = item.get("values", {}).get("name", [])
            name = ""
            if name_values and isinstance(name_values, list):
                active = [e for e in name_values if e.get("active_until") is None]
                entry = active[0] if active else name_values[0]
                name = entry.get("value", "")

            results.append({
                "record_id": record_id,
                "name": name,
                "object_slug": "companies",
            })

        return results

    def get_deal_entry(self, record_id: str, list_slug: str | None = None) -> dict | None:
        """Find the deal list entry for a company record.

        Uses the record entries endpoint to discover which lists the
        record belongs to, then fetches the full entry values by ID.

        Args:
            record_id: UUID of the parent company record.
            list_slug: Optional list slug to match. If None, uses the
                first list entry found for the record.

        Returns:
            Dict with flattened entry values, ``entry_id``, and
            ``list_api_slug``, or None if no entry is found.
        """
        try:
            # Step 1: Find entry via the record entries endpoint
            response = self._client.get(
                f"/objects/companies/records/{record_id}/entries",
                params={"limit": 100},
            )
            response.raise_for_status()

            entries = response.json().get("data", [])
            if not entries:
                return None

            # Match by slug if provided, otherwise use the first entry
            target = None
            if list_slug:
                for e in entries:
                    if e.get("list_api_slug") == list_slug:
                        target = e
                        break
            if not target:
                target = entries[0]

            target_entry_id = target.get("entry_id")
            resolved_slug = target.get("list_api_slug")

            if not target_entry_id or not resolved_slug:
                return None

            # Step 2: Fetch full entry with values by ID
            response = self._client.get(
                f"/lists/{resolved_slug}/entries/{target_entry_id}",
            )
            response.raise_for_status()
            entry = response.json().get("data", {})
            # Attio returns list entry fields under "entry_values", not "values"
            raw_values = entry.get("entry_values") or entry.get("values") or {}
            values = _flatten_values(raw_values)
            values["entry_id"] = target_entry_id
            values["list_api_slug"] = resolved_slug
            return values

        except Exception as e:
            logger.warning("Failed to fetch deal entry for %s: %s", record_id, e)
            return None

    def update_deal_entry(
        self,
        entry_id: str,
        updates: dict,
        list_slug: str | None = None,
    ) -> None:
        """Write field values back to a deal entry.

        Only updates fields that have new non-None values.

        Args:
            entry_id: UUID of the list entry.
            updates: Dict of {attio_field_slug: value} to write.
            list_slug: Attio list slug. Required — pass the
                ``list_api_slug`` returned by :meth:`get_deal_entry`.
        """
        if not list_slug:
            logger.warning("No list_slug provided for update_deal_entry, skipping")
            return

        # Filter out None/empty values
        filtered = {k: v for k, v in updates.items() if v is not None and v != ""}

        if not filtered:
            logger.info("No non-empty values to write back to Attio")
            return

        try:
            response = self._client.patch(
                f"/lists/{list_slug}/entries/{entry_id}",
                json={"values": filtered},
            )
            response.raise_for_status()
            logger.info("Updated Attio deal entry %s: %s", entry_id, list(filtered.keys()))
        except Exception as e:
            logger.warning("Failed to update deal entry %s: %s", entry_id, e)

    def extract_document_urls_from_notes(
        self, record_id: str, exclude_url: str | None = None,
    ) -> list[dict]:
        """Scan all notes for a company and extract document URLs.

        Looks for Google Drive links, DocSend links, and direct PDF links
        in note markdown content.

        Args:
            record_id: UUID of the company record.
            exclude_url: URL to exclude (e.g. the deal's pitch_deck_link).

        Returns:
            List of dicts with: url, source ("attio_note"), note_title.
        """
        notes = self.get_notes(record_id)

        url_patterns = [
            r"https?://drive\.google\.com/file/d/[a-zA-Z0-9_-]+[^\s\)\"]*",
            r"https?://docs\.google\.com/(?:presentation|document|spreadsheets)/d/[a-zA-Z0-9_-]+[^\s\)\"]*",
            r"https?://drive\.google\.com/open\?id=[a-zA-Z0-9_-]+[^\s\)\"]*",
            r"https?://(?:www\.)?docsend\.com/view/[a-zA-Z0-9_-]+[^\s\)\"]*",
            r"https?://[^\s\)\"]+\.pdf(?:\?[^\s\)\"]*)?",
        ]
        combined_pattern = "|".join(f"({p})" for p in url_patterns)

        results = []
        seen_urls: set[str] = set()

        # Normalize exclude URL for comparison
        exclude_normalized = exclude_url.rstrip("/").lower() if exclude_url else None

        for note in notes:
            content = note.get("content_markdown", "") or note.get("content_plaintext", "")
            if not content:
                continue

            for match in re.finditer(combined_pattern, content):
                url = match.group(0).rstrip(".,;)")
                url_normalized = url.rstrip("/").lower()

                if url_normalized in seen_urls:
                    continue
                if exclude_normalized and url_normalized == exclude_normalized:
                    continue

                seen_urls.add(url_normalized)
                results.append({
                    "url": url,
                    "source": "attio_note",
                    "note_title": note.get("title", "Untitled"),
                })

        logger.info(
            "Found %d document URLs in notes for record %s", len(results), record_id,
        )
        return results

    def search_and_get_company(self, query: str) -> dict | None:
        """Search for a company by domain (preferred) or name, and return full details.

        If the query contains a dot, it's treated as a domain and searched
        via the records/query filter. Otherwise falls back to fuzzy name search.

        Args:
            query: Domain (e.g., "lazo.us") or company name.

        Returns:
            Dict with: record_id, name, web_url, values, transcripts,
            deal (dict or None), deck_url (str or None).
            None if company not found.
        """
        if "." in query:
            results = self.search_by_domain(query)
        else:
            results = self.search_companies(query, limit=1)

        if not results:
            # If domain search failed, try name search as fallback
            if "." in query:
                name_part = query.split(".")[0]
                results = self.search_companies(name_part, limit=1)
            if not results:
                return None

        top = results[0]
        record_id = top["record_id"]

        company = self.get_company(record_id)
        company["name"] = top.get("name", "")
        company["transcripts"] = self.find_transcripts(record_id)

        # Fetch deal data from the All Deals list
        deal = self.get_deal_entry(record_id)
        company["deal"] = deal
        company["deck_url"] = None
        if deal:
            # Extract deck URL from Pitch Deck Link field
            deck_link = deal.get("pitch_deck_link") or deal.get("pitch_deck") or deal.get("deck_link")
            if deck_link:
                company["deck_url"] = deck_link

        return company

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _flatten_values(values: dict[str, Any]) -> dict[str, Any]:
    """Flatten Attio's nested values structure into simple key-value pairs.

    Attio returns values as arrays of versioned entries. This extracts
    the most recent active value for each attribute.
    """
    flat = {}
    for attr_name, entries in values.items():
        if not isinstance(entries, list) or not entries:
            flat[attr_name] = None
            continue

        # Get the most recent active entry
        active = [e for e in entries if e.get("active_until") is None]
        entry = active[0] if active else entries[0]

        # Extract the actual value based on attribute type
        attr_type = entry.get("attribute_type", "")

        if attr_type == "text":
            flat[attr_name] = entry.get("value", "")
        elif attr_type == "number":
            flat[attr_name] = entry.get("value")
        elif attr_type == "email":
            flat[attr_name] = entry.get("email_address", "")
        elif attr_type == "domain":
            flat[attr_name] = entry.get("domain", "")
        elif attr_type == "phone-number":
            flat[attr_name] = entry.get("phone_number", "")
        elif attr_type in ("select", "status"):
            flat[attr_name] = entry.get("option", {}).get("title", "")
        elif attr_type == "record-reference":
            flat[attr_name] = entry.get("target_record_id", "")
        elif attr_type == "currency":
            flat[attr_name] = entry.get("currency_value")
        elif attr_type == "date":
            flat[attr_name] = entry.get("value")
        elif attr_type == "checkbox":
            flat[attr_name] = entry.get("value", False)
        elif attr_type == "rating":
            flat[attr_name] = entry.get("value")
        elif attr_type == "location":
            flat[attr_name] = entry.get("line_1", "")
        elif attr_type == "personal-name":
            first = entry.get("first_name", "")
            last = entry.get("last_name", "")
            flat[attr_name] = f"{first} {last}".strip()
        elif attr_type == "interaction":
            flat[attr_name] = entry.get("interacted_at")
        else:
            # Fallback: try common value keys
            flat[attr_name] = entry.get("value", entry.get("original_value"))

    return flat


def is_configured() -> bool:
    """Check if Attio API key is configured."""
    return bool(os.environ.get("ATTIO_API_KEY"))
