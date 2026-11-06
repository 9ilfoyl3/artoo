"""Minimal task-list tool for multi-step Agent work."""

from __future__ import annotations

from xml.sax.saxutils import escape as xml_escape

from app.agent.state import AgentState
from app.agent.tools.base import BaseTool, ToolResult

_VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoWriteTool(BaseTool):
    """Replace the current task list with a small, explicit plan."""

    # This mutates Agent state, so the scheduler serializes it against reads.
    read_only = False

    def __init__(self, state: AgentState | None = None) -> None:
        self._state = state

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Create or update a short task list for the current turn. Use this "
            "only when a request genuinely has multiple dependent steps. Keep "
            "at most one item in_progress. Todos are planning state, not evidence."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": sorted(_VALID_STATUSES),
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def execute(self, args: dict) -> ToolResult:
        raw_todos = args.get("todos")
        if not isinstance(raw_todos, list) or not raw_todos:
            return ToolResult(success=False, error="todos must be a non-empty array")
        if len(raw_todos) > 20:
            return ToolResult(success=False, error="todos supports at most 20 items")

        normalized: list[dict[str, str]] = []
        in_progress = 0
        for raw in raw_todos:
            if not isinstance(raw, dict):
                return ToolResult(success=False, error="each todo must be an object")
            content = str(raw.get("content") or "").strip()
            status = str(raw.get("status") or "").strip()
            if not content:
                return ToolResult(success=False, error="todo content must not be empty")
            if status not in _VALID_STATUSES:
                return ToolResult(
                    success=False,
                    error="todo status must be pending, in_progress, or completed",
                )
            if status == "in_progress":
                in_progress += 1
            normalized.append({"content": content[:500], "status": status})

        if in_progress > 1:
            return ToolResult(
                success=False,
                error="at most one todo may be in_progress",
            )

        if self._state is not None:
            self._state.todos = [dict(todo) for todo in normalized]

        lines = ['<todos>']
        for index, todo in enumerate(normalized, start=1):
            lines.append(
                f'<todo index="{index}" status="{xml_escape(todo["status"])}">'
                f'{xml_escape(todo["content"])}</todo>'
            )
        lines.append("</todos>")
        lines.append(
            "[Todo state updated. Use task progress to guide the next action, "
            "but rely on retrieved evidence for factual claims.]"
        )
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={"todos": normalized, "count": len(normalized)},
        )


__all__ = ["TodoWriteTool"]
