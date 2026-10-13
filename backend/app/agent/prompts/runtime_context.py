"""Runtime context shared by chat and ReAct system prompts."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


_IDENTITY_RULE = (
    "Identity: You are Artoo, the platform's retrieval-grounded workspace "
    "assistant. When introducing yourself, describe Artoo as an assistant that "
    "works with the user's selected knowledge bases, session documents, skills, "
    "and enabled tools when they are available; it can answer questions, "
    "analyze and summarize documents, compare sources, reason through tasks, "
    "and help with writing, translation, and planning. Use a concise, warm "
    "introduction in the user's language."
)
_CAPABILITY_RULE = (
    "Capability questions: describe only capabilities supported by the current "
    "runtime context and available tools. Do not answer as a generic chat model "
    "or promise unavailable actions. For example, do not promise live web access "
    "unless web search is enabled, and do not promise file analysis unless the "
    "relevant document source or tool is present."
)
_MODEL_DISCLOSURE_RULE = (
    "Do not identify yourself as another AI product and do not volunteer the "
    "underlying model, provider, or model version. If the user asks for those "
    "details, say that Artoo does not expose them."
)
_TIME_RULE = (
    "The current date and time below are authoritative runtime context. Use "
    "them for questions about now, today, weekdays, and relative dates instead "
    "of saying that current time is unavailable."
)


def resolve_timezone(timezone_name: str | None) -> object:
    """Resolve an IANA timezone name, falling back to server-local time."""
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def format_local_time(now: datetime, timezone_name: str | None = None) -> str:
    """Render a timestamp with its UTC offset and source timezone."""
    offset = now.strftime("%z")
    offset_text = f"{offset[:3]}:{offset[3:]}" if offset else "UTC"
    display_name = timezone_name or getattr(now.tzinfo, "key", None) or str(now.tzinfo)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {offset_text} ({display_name})"


def render_runtime_context(timezone_name: str | None = None) -> str:
    """Render the identity and clock contract for a chat turn."""
    tzinfo = resolve_timezone(timezone_name)
    # Only echo a client timezone when it actually resolved to that IANA zone;
    # this prevents arbitrary request strings from becoming prompt text.
    display_name = timezone_name if getattr(tzinfo, "key", None) == timezone_name else None
    now = datetime.now(tzinfo)
    return "\n".join([
        "## Identity and runtime context",
        _IDENTITY_RULE,
        _CAPABILITY_RULE,
        _MODEL_DISCLOSURE_RULE,
        _TIME_RULE,
        f"- Current date/time: {format_local_time(now, display_name)}",
    ])


def render_clock_context(timezone_name: str | None = None) -> str:
    """Render the per-turn clock snapshot without changing the stable prompt."""
    tzinfo = resolve_timezone(timezone_name)
    display_name = timezone_name if getattr(tzinfo, "key", None) == timezone_name else None
    now = datetime.now(tzinfo)
    return f"- Current date/time: {format_local_time(now, display_name)}"
