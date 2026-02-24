from src.ingestion.document_processor import extract_from_document
from src.ingestion.deck_fetcher import fetch_deck
from src.ingestion.merger import merge_extractions

__all__ = ["extract_from_document", "fetch_deck", "merge_extractions"]
