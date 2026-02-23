from __future__ import annotations

"""Lightweight API call tracing for the memo-agent pipeline.

Wraps an ``anthropic.Anthropic`` client so that every ``messages.create()``
call is logged to a JSONL trace file (one JSON object per line).  The wrapper
is transparent — callers interact with the same interface and receive the
same responses.

Usage::

    from src.tracing import create_traced_client

    client = create_traced_client(output_dir="data/output/lazo/")
    # use client exactly like anthropic.Anthropic()
"""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import anthropic


class TracedMessages:
    """Wrapper around ``client.messages`` that logs every ``.create()`` call."""

    def __init__(self, real_messages, trace_file: Path):
        self._messages = real_messages
        self._trace_file = trace_file

    def create(self, **kwargs):
        start = time.time()
        response = self._messages.create(**kwargs)
        elapsed = time.time() - start

        # Hash the system prompt for grouping without storing full text
        system = kwargs.get("system") or ""
        if isinstance(system, list):
            # Handle structured system prompt (list of content blocks)
            system = json.dumps(system, sort_keys=True)
        system_hash = hashlib.md5(system.encode()).hexdigest()[:8]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": kwargs.get("model"),
            "system_prompt_hash": system_hash,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": round(elapsed * 1000),
            "max_tokens": kwargs.get("max_tokens"),
            "stop_reason": response.stop_reason,
        }

        with open(self._trace_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        return response

    def __getattr__(self, name):
        return getattr(self._messages, name)


class TracedClient:
    """Drop-in wrapper for ``anthropic.Anthropic`` that traces API calls."""

    def __init__(self, client: anthropic.Anthropic, trace_file: Path):
        self._client = client
        self.messages = TracedMessages(client.messages, trace_file)

    def __getattr__(self, name):
        return getattr(self._client, name)


def create_traced_client(
    output_dir: str | Path | None = None,
    run_id: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> anthropic.Anthropic | TracedClient:
    """Create an Anthropic client, optionally wrapped with tracing.

    Tracing activates when ``output_dir`` is provided.  The trace file is
    written to ``{output_dir}/traces/trace_{timestamp}.jsonl``.

    Args:
        output_dir: Pipeline output directory.  When provided, enables tracing.
        run_id: Optional identifier embedded in the trace filename.
        client: Pre-existing Anthropic client to wrap.  A new one is created
            if not supplied.

    Returns:
        A plain ``anthropic.Anthropic`` (no tracing) or a ``TracedClient``.
    """
    if client is None:
        client = anthropic.Anthropic()

    if output_dir is None:
        return client

    traces_dir = Path(output_dir) / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{run_id}" if run_id else ""
    trace_file = traces_dir / f"trace_{ts}{suffix}.jsonl"

    return TracedClient(client, trace_file)


def summarize_trace(trace_file: str | Path) -> dict:
    """Read a JSONL trace file and return aggregate statistics.

    Returns:
        Dict with total_calls, total_input_tokens, total_output_tokens,
        total_latency_ms, and models used.
    """
    trace_file = Path(trace_file)
    if not trace_file.exists():
        return {}

    total_input = 0
    total_output = 0
    total_latency = 0
    call_count = 0
    models = set()

    with open(trace_file) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            total_input += entry.get("input_tokens", 0)
            total_output += entry.get("output_tokens", 0)
            total_latency += entry.get("latency_ms", 0)
            call_count += 1
            if entry.get("model"):
                models.add(entry["model"])

    return {
        "total_calls": call_count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_latency_ms": total_latency,
        "models": sorted(models),
    }
