"""Retry wrapper for Anthropic API calls with exponential backoff.

Handles transient API errors like rate limits (429) and overloaded (529) by
retrying with exponential backoff.  Used by TracedClient and RetryClient to
provide transparent retry behaviour for all pipeline API calls.
"""

from __future__ import annotations

import logging
import time

import anthropic

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 4
INITIAL_DELAY = 2.0  # seconds
BACKOFF_FACTOR = 2.0  # each retry doubles the delay: 2s, 4s, 8s, 16s
RETRYABLE_STATUS_CODES = {429, 529}


def call_api_with_retry(create_fn, **kwargs):
    """Call an API function with exponential backoff on transient errors.

    Retries on rate-limit (429) and overloaded (529) API errors with delays
    of 2s, 4s, 8s, 16s between attempts.

    Args:
        create_fn: Callable (e.g., ``client.messages.create``).
        **kwargs: Arguments forwarded to *create_fn*.

    Returns:
        The API response from *create_fn*.

    Raises:
        anthropic.APIStatusError: Re-raised after all retries are exhausted,
            or immediately for non-retryable status codes.
    """
    last_exception = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return create_fn(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise
            last_exception = e
            if attempt < MAX_RETRIES:
                delay = INITIAL_DELAY * (BACKOFF_FACTOR ** attempt)
                logger.warning(
                    "API call failed (attempt %d/%d, status %d): %s. "
                    "Retrying in %.1fs...",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    e.status_code,
                    str(e)[:200],
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "API call failed after %d attempts (status %d)",
                    MAX_RETRIES + 1,
                    e.status_code,
                )
    raise last_exception


class RetryMessages:
    """Wraps ``client.messages`` to add retry logic on ``.create()``."""

    def __init__(self, messages):
        self._messages = messages

    def create(self, **kwargs):
        return call_api_with_retry(self._messages.create, **kwargs)

    def __getattr__(self, name):
        return getattr(self._messages, name)


class RetryClient:
    """Drop-in wrapper for ``anthropic.Anthropic`` with retry on transient errors.

    Usage::

        from src.api_retry import RetryClient
        client = RetryClient(anthropic.Anthropic())
        # use client exactly like anthropic.Anthropic()
    """

    def __init__(self, client: anthropic.Anthropic):
        self._client = client
        self.messages = RetryMessages(client.messages)

    def __getattr__(self, name):
        return getattr(self._client, name)
