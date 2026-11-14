"""Chat message role normalization shared by Agent memory paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def normalize_system_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse every system message into one leading system message.

    OpenAI-compatible providers require system messages to appear only at the
    beginning of the conversation. Memory consolidation used to append a second
    system message after the original prompt, and older checkpoints may still
    contain that shape. Summaries are therefore appended to the first system
    message instead of being sent as separate system turns.
    """

    copied = [dict(message) for message in messages]
    system_messages = [
        message for message in copied if message.get("role") == "system"
    ]
    if not system_messages:
        return copied

    system_content: list[str] = []
    for message in system_messages:
        content = message.get("content")
        if isinstance(content, str):
            if content:
                system_content.append(content)
        elif content is not None:
            system_content.append(str(content))

    leading_system = dict(system_messages[0])
    leading_system["content"] = "\n\n".join(system_content)
    return [
        leading_system,
        *[message for message in copied if message.get("role") != "system"],
    ]
