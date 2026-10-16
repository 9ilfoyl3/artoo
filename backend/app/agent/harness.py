"""Deterministic policy helpers shared by the Agent loop and retrieval tools."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

REPEAT_TOOL_REMINDER_THRESHOLDS = (3, 5, 8)
_RETRIEVAL_TOOLS = {"search_knowledge"}
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_query(query: object) -> str:
    """Return a stable retrieval-query form without changing semantics."""
    if not isinstance(query, str):
        return ""
    normalized = unicodedata.normalize("NFKC", query).strip()
    return _WHITESPACE_RE.sub(" ", normalized)


def dedupe_queries(queries: object, limit: int = 3) -> list[str]:
    """Normalize, deduplicate, and bound a tool call's query list."""
    if not isinstance(queries, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in queries:
        query = normalize_query(raw)
        if not query or query.casefold() in seen:
            continue
        seen.add(query.casefold())
        result.append(query)
        if len(result) >= limit:
            break
    return result


def canonical_tool_signature(tool_name: str, arguments: object) -> str:
    """Build an exact-match signature; retrieval tools also normalize query order."""
    normalized = arguments
    if isinstance(arguments, dict) and tool_name in _RETRIEVAL_TOOLS:
        normalized = dict(arguments)
        if "queries" in normalized:
            normalized["queries"] = sorted({
                query.casefold()
                for query in dedupe_queries(normalized.get("queries"), limit=10)
            })
        if "query" in normalized:
            normalized["query"] = normalize_query(normalized.get("query")).casefold()
        if "knowledge_base_ids" in normalized:
            value = normalized.get("knowledge_base_ids")
            if isinstance(value, list):
                normalized["knowledge_base_ids"] = sorted(str(item) for item in value)
    return json.dumps(
        [tool_name, normalized],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass
class RepeatToolTracker:
    """Track one Agent's consecutive exact tool-call chain."""

    _key: str | None = None
    _count: int = 0

    def observe(self, tool_name: str, arguments: object) -> str | None:
        key = canonical_tool_signature(tool_name, arguments)
        if key == self._key:
            self._count += 1
        else:
            self._key = key
            self._count = 1
        if self._count not in REPEAT_TOOL_REMINDER_THRESHOLDS:
            return None
        if self._count == REPEAT_TOOL_REMINDER_THRESHOLDS[0]:
            return (
                "You are repeating the exact same tool call. Inspect the previous "
                "result and either use different arguments or finish the task."
            )
        return (
            f"Repeated tool call detected: {tool_name} x{self._count}. "
            "The repeated calls are not making progress. Do not repeat these exact "
            "arguments again; inspect the latest result and choose a different "
            "action, different arguments, or finish if enough evidence was gathered."
        )

    def reset(self) -> None:
        self._key = None
        self._count = 0
