"""MCP structuredContent 的通用校验与边界控制。

Artoo 不解释任何业务字段。这里只保证进入 Agent 事件和持久化层的结构化结果：

- 是合法 JSON；
- 使用 ``{"kind": "...", "data": ...}`` 通用 envelope；
- 不超过内存、深度、数组和字符串上限；
- 如果 MCP 声明了 ``outputSchema``，结果符合该 schema。

原始 ``content[].text`` 仍只进入模型上下文，不经过本模块外发。
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

MAX_STRUCTURED_CONTENT_BYTES = 64 * 1024
MAX_OUTPUT_SCHEMA_BYTES = 64 * 1024
MAX_STRUCTURED_DEPTH = 8
MAX_STRUCTURED_ARRAY_ITEMS = 100
MAX_STRUCTURED_STRING_CHARS = 16 * 1024
MAX_STRUCTURED_OBJECT_KEYS = 128
MAX_STRUCTURED_TOTAL_OBJECT_KEYS = 1024
MAX_FEEDBACK_ACTIONS = 10
MAX_FEEDBACK_ACTION_CHARS = 200
MAX_FEEDBACK_MESSAGE_CHARS = 4_096

# kind 是 renderer registry 的稳定键。要求全局命名空间，避免不同 MCP Server
# 使用 document_diff.v1 这类泛化名称时发生渲染器冲突。
_KIND_RE = re.compile(r"^[a-z][a-z0-9._-]{2,127}$")
_FORBIDDEN_OBJECT_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_FEEDBACK_STATUSES = frozenset({
    "accepted",
    "rejected",
    "needs_evidence",
    "retryable_error",
    "completed",
})


class StructuredResultValidationError(ValueError):
    """结构化结果或 outputSchema 不满足通用契约。"""


def normalize_output_schema(value: Any) -> dict[str, Any] | None:
    """校验并返回 JSON Schema 的独立副本；未声明时返回 ``None``。"""
    if value is None:
        return None

    schema = _normalize_json(value, "outputSchema", MAX_OUTPUT_SCHEMA_BYTES)
    if not isinstance(schema, dict):
        raise StructuredResultValidationError("outputSchema 必须是 JSON object")

    _reject_external_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise StructuredResultValidationError(
            f"outputSchema 不是合法 JSON Schema: {exc.message}"
        ) from exc
    return schema


def normalize_structured_content(
    value: Any,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验并返回 structuredContent 的独立 JSON 副本。

    ``outputSchema`` 只用于校验，不参与业务字段解释。校验失败时调用方应丢弃
    structuredContent，同时保留给模型的文本输出。
    """
    content = _normalize_json(value, "structuredContent", MAX_STRUCTURED_CONTENT_BYTES)
    if not isinstance(content, dict):
        raise StructuredResultValidationError("structuredContent 必须是 JSON object")

    kind = content.get("kind")
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise StructuredResultValidationError(
            "structuredContent.kind 必须是 3-128 位全局命名空间标识"
        )
    if output_schema is not None:
        try:
            Draft202012Validator(output_schema).validate(content)
        except ValidationError as exc:
            raise StructuredResultValidationError(
                f"structuredContent 不符合 outputSchema: {exc.message}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 远端 schema 不得拖垮文本工具结果
            raise StructuredResultValidationError(
                f"outputSchema 校验失败: {exc}"
            ) from exc

    if "data" not in content:
        raise StructuredResultValidationError("structuredContent.data 缺失")
    _validate_feedback(content.get("feedback"))

    return content


def _validate_feedback(value: Any) -> None:
    """Validate the optional generic feedback envelope without interpreting it."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise StructuredResultValidationError("structuredContent.feedback 必须是 object")

    status = value.get("status")
    if status not in _FEEDBACK_STATUSES:
        allowed = ", ".join(sorted(_FEEDBACK_STATUSES))
        raise StructuredResultValidationError(
            f"structuredContent.feedback.status 必须是: {allowed}"
        )

    code = value.get("code")
    if code is not None:
        if not isinstance(code, str) or not _KIND_RE.fullmatch(code):
            raise StructuredResultValidationError(
                "structuredContent.feedback.code 必须是 3-128 位稳定标识"
            )

    message = value.get("message")
    if message is not None:
        if not isinstance(message, str):
            raise StructuredResultValidationError(
                "structuredContent.feedback.message 必须是 string"
            )
        if len(message) > MAX_FEEDBACK_MESSAGE_CHARS:
            raise StructuredResultValidationError(
                "structuredContent.feedback.message 超过长度上限"
            )

    recoverable = value.get("recoverable")
    if recoverable is not None and not isinstance(recoverable, bool):
        raise StructuredResultValidationError(
            "structuredContent.feedback.recoverable 必须是 boolean"
        )

    next_actions = value.get("next_actions")
    if next_actions is not None:
        if not isinstance(next_actions, list) or len(next_actions) > MAX_FEEDBACK_ACTIONS:
            raise StructuredResultValidationError(
                f"structuredContent.feedback.next_actions 最多 {MAX_FEEDBACK_ACTIONS} 项"
            )
        for action in next_actions:
            if (
                not isinstance(action, str)
                or not action.strip()
                or len(action) > MAX_FEEDBACK_ACTION_CHARS
            ):
                raise StructuredResultValidationError(
                    "structuredContent.feedback.next_actions 必须是短字符串数组"
                )


def _normalize_json(value: Any, label: str, max_bytes: int) -> Any:
    """序列化往返，确保结果只包含标准 JSON 类型且没有循环引用。"""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StructuredResultValidationError(f"{label} 不是合法 JSON") from exc

    if len(encoded.encode("utf-8")) > max_bytes:
        raise StructuredResultValidationError(
            f"{label} 超过 {max_bytes} bytes 上限"
        )

    normalized = json.loads(encoded)
    _check_limits(normalized, label)
    return normalized


def _check_limits(value: Any, label: str) -> None:
    total_object_keys = 0
    stack: list[tuple[Any, int]] = [(value, 0)]

    while stack:
        current, depth = stack.pop()
        if depth > MAX_STRUCTURED_DEPTH:
            raise StructuredResultValidationError(
                f"{label} 嵌套深度超过 {MAX_STRUCTURED_DEPTH}"
            )

        if isinstance(current, str):
            if len(current) > MAX_STRUCTURED_STRING_CHARS:
                raise StructuredResultValidationError(
                    f"{label} 字符串超过 {MAX_STRUCTURED_STRING_CHARS} 字符"
                )
            continue

        if isinstance(current, list):
            if len(current) > MAX_STRUCTURED_ARRAY_ITEMS:
                raise StructuredResultValidationError(
                    f"{label} 数组长度超过 {MAX_STRUCTURED_ARRAY_ITEMS}"
                )
            stack.extend((item, depth + 1) for item in current)
            continue

        if isinstance(current, dict):
            if len(current) > MAX_STRUCTURED_OBJECT_KEYS:
                raise StructuredResultValidationError(
                    f"{label} object 字段数超过 {MAX_STRUCTURED_OBJECT_KEYS}"
                )
            total_object_keys += len(current)
            if total_object_keys > MAX_STRUCTURED_TOTAL_OBJECT_KEYS:
                raise StructuredResultValidationError(
                    f"{label} 总字段数超过 {MAX_STRUCTURED_TOTAL_OBJECT_KEYS}"
                )
            for key, item in current.items():
                if key in _FORBIDDEN_OBJECT_KEYS:
                    raise StructuredResultValidationError(
                        f"{label} 包含禁止字段: {key}"
                    )
                stack.append((item, depth + 1))


def _reject_external_refs(value: Any) -> None:
    """禁止远程/文件 ``$ref``，避免校验期间产生隐式网络或文件访问。"""
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
                raise StructuredResultValidationError(
                    "outputSchema 仅允许本地 #/... $ref"
                )
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
