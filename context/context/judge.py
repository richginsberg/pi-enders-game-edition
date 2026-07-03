"""Salience judge: extract durable facts from a transcript worth long-term memory.

A cheap fleet model (S3 by default) reads a transcript and returns only the facts
worth persisting — decisions, constraints, outcomes, handoffs — skipping chatter.
This is the gate that keeps the context store signal-dense: it runs at milestone /
session end, never blindly on every message.

Pure helpers (`build_judge_messages`, `build_chat_request`, `parse_judge_output`)
are unit-testable; `distill` does the LLM call.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from .models import Kind

LLM_BASE = os.environ.get("DNC_LLM_BASE", "http://localhost:4000/v1")
JUDGE_MODEL = os.environ.get("DNC_JUDGE_MODEL", "tier:s3")
LLM_KEY = os.environ.get("DNC_LLM_KEY")

_KINDS = ", ".join(str(k) for k in Kind)

JUDGE_SYSTEM = f"""You extract durable facts from a coding session for long-term memory.

Return ONLY a JSON array. Each element: {{"kind": <one of: {_KINDS}>, "text": <one sentence>}}.

Keep a fact ONLY if it will still matter in a future session on this repo:
- decision: a choice made and its reason (e.g. "Chose pgvector over sqlite-vec for the context store").
- constraint: a rule or limit the project must respect (e.g. "Never commit local endpoints; env vars only").
- outcome: a concrete result (e.g. "The 8-thread embed run pegged the CPU and starved the proxy").
- handoff: unfinished state to resume later (e.g. "LiteLLM registration flip after migration is still unimplemented").

DROP: greetings, restated questions, transient status, tool noise, and anything already
obvious from the code. Each fact must be self-contained — no "it"/"this" without a clear
subject. If nothing is durable, return []. Be terse; prefer fewer, higher-value facts."""


def build_judge_messages(transcript: str) -> list[dict]:
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"Transcript:\n\n{transcript}\n\nReturn the JSON array of durable facts."},
    ]


def build_chat_request(model: str, messages: list[dict], temperature: float = 0.0) -> dict:
    return {"model": model, "messages": messages, "temperature": temperature}


def parse_judge_output(text: str) -> list[tuple[Kind, str]]:
    """Extract validated (kind, text) pairs from the model's reply.

    Tolerant of prose/code-fence wrapping: pulls the outermost JSON array. Drops
    malformed entries and unknown kinds rather than failing the whole batch.
    """
    if not text:
        return []
    stripped = re.sub(r"```(?:json)?|```", "", text)
    start, end = stripped.find("["), stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        raw = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return []
    facts: list[tuple[Kind, str]] = []
    kinds = {str(k) for k in Kind}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind, fact = item.get("kind"), item.get("text")
        if kind in kinds and isinstance(fact, str) and fact.strip():
            facts.append((Kind(kind), fact.strip()))
    return facts


def _post_chat(base: str, body: dict, key: str | None) -> str:
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), method="POST")
    req.add_header("content-type", "application/json")
    if key and key != "none":
        req.add_header("authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"]["content"] or ""


def distill(
    transcript: str, *, base: str = LLM_BASE, model: str = JUDGE_MODEL, key: str | None = LLM_KEY
) -> list[tuple[Kind, str]]:
    """Run the judge over a transcript, returning durable facts (possibly empty)."""
    if not transcript.strip():
        return []
    content = _post_chat(base, build_chat_request(model, build_judge_messages(transcript)), key)
    return parse_judge_output(content)
