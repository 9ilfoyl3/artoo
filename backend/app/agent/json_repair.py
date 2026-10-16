"""Bounded JSON repair for model-generated tool arguments and structured output.

The repair path is intentionally conservative:

- It only extracts a JSON object/array from fenced or prose-wrapped text.
- It can remove trailing commas and close unclosed containers.
- It never invents missing fields or values.
- Callers can inspect ``truncated`` and reject execution when the source was
  cut off by the model/provider.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class JSONRepairResult:
    """Result of a conservative JSON parse/repair attempt."""

    value: object | None
    error: str | None = None
    repaired: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.value is not None and self.error is None


def _extract_candidate(raw: str) -> str:
    """Extract the first balanced-looking JSON object or array from text."""
    text = raw.strip()
    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    start = min(
        (index for index in (text.find("{"), text.find("[")) if index >= 0),
        default=-1,
    )
    if start < 0:
        return text
    return text[start:]


def _scan_balance(text: str) -> tuple[int, bool, bool]:
    """Return ``(depth, in_string, escaped)`` for a JSON-like fragment."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
    return depth, in_string, escaped


def _close_open_containers(text: str) -> tuple[str, bool]:
    """Close only containers that are still open, preserving string state."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]" and stack and stack[-1] == char:
            stack.pop()

    truncated = in_string or bool(stack)
    if in_string:
        text += '"'
    while stack:
        text += stack.pop()
    return text, truncated


def _remove_trailing_commas(text: str) -> str:
    """Remove commas immediately before a closing container."""
    return re.sub(r",\s*([}\]])", r"\1", text)


def parse_json_with_repair(raw: str) -> JSONRepairResult:
    """Parse JSON, then apply conservative repair only when needed.

    The function accepts objects and arrays. A repaired value is still returned
    when it parses successfully, but ``truncated=True`` tells callers not to
    execute side-effecting tools because the model output may be incomplete.
    """
    if not raw or not raw.strip():
        return JSONRepairResult(value={}, repaired=False)

    try:
        return JSONRepairResult(value=json.loads(raw), repaired=False)
    except (json.JSONDecodeError, TypeError) as original_error:
        candidate = _extract_candidate(raw)
        if not candidate:
            return JSONRepairResult(
                value=None,
                error=str(original_error),
                repaired=False,
                truncated=False,
            )

        repaired_text, truncated = _close_open_containers(candidate)
        repaired_text = _remove_trailing_commas(repaired_text)
        try:
            value = json.loads(repaired_text)
        except (json.JSONDecodeError, TypeError) as repair_error:
            return JSONRepairResult(
                value=None,
                error=str(repair_error),
                repaired=True,
                truncated=truncated,
            )

        if not isinstance(value, (dict, list)):
            return JSONRepairResult(
                value=None,
                error="repaired JSON must be an object or array",
                repaired=True,
                truncated=truncated,
            )
        return JSONRepairResult(
            value=value,
            error=None,
            repaired=True,
            truncated=truncated,
        )


def parse_tool_arguments(raw: str) -> JSONRepairResult:
    """Parse a tool's JSON arguments and normalize ``{}`` for empty input."""
    result = parse_json_with_repair(raw)
    if result.ok and not isinstance(result.value, dict):
        return JSONRepairResult(
            value=None,
            error="tool arguments must be a JSON object",
            repaired=result.repaired,
            truncated=result.truncated,
        )
    return result


__all__ = [
    "JSONRepairResult",
    "parse_json_with_repair",
    "parse_tool_arguments",
]
