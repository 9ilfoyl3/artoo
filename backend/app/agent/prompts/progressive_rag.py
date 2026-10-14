"""System prompt for the DSH-style ReAct retrieval loop."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from app.agent.prompts.runtime_context import (
    _CAPABILITY_RULE,
    _IDENTITY_RULE,
    _MODEL_DISCLOSURE_RULE,
    format_local_time,
    resolve_timezone,
)

if TYPE_CHECKING:
    from app.agent.config import AgentConfig


SYSTEM_PROMPT_PLACEHOLDERS = {
    "knowledge_base_names": "当前绑定的知识库名称列表",
    "available_tools": "当前 Agent 可用的工具列表",
    "available_skills": "当前可按需加载的技能列表",
    "web_search_status": "网络搜索是否启用",
    "runtime_clock": "当前日期与时间",
    "user_customization": "管理员配置的角色/语气/工作风格/边界",
}

_LANGUAGE_DIRECTIVES = {
    "zh": (
        "最终回答必须使用中文，并与用户当前使用的书面形式保持一致（简体或繁体），"
        "不因工具结果或历史消息是英文而切换。"
        "推理过程保持简短；可优先使用中文，但准确性、代码、工具名、参数、文件路径"
        "和引用原文不受语言限制。"
    ),
    "ja": (
        "最終回答は日本語で統一し、ツール結果や履歴が英語でも言語を切り替えないでください。"
        "推論は簡潔にし、日本語を優先して構いませんが、正確性、コード、ツール名、引数、"
        "ファイルパス、引用原文は原語のままで構いません。"
    ),
    "ko": (
        "최종 답변은 한국어로 작성하고, 도구 결과나 이전 메시지가 영어여도 언어를 바꾸지 마세요. "
        "추론은 짧게 유지하고 한국어를 우선할 수 있지만, 정확성, 코드, 도구 이름, 인수, "
        "파일 경로, 인용문은 원문 그대로 둘 수 있습니다."
    ),
    "en": (
        "The final answer must use English and remain in English even when tool results "
        "or history contain another language. Keep reasoning concise; prefer English "
        "when practical, but accuracy, code, tool names, arguments, file paths, and "
        "quotations may remain verbatim."
    ),
}

_PROMPT = """\
You are Artoo, a retrieval-grounded assistant running in a ReAct loop.

## Identity and runtime context
- {_IDENTITY_RULE}
- {_CAPABILITY_RULE}
- {_MODEL_DISCLOSURE_RULE}
- The current date/time below is authoritative runtime context. Use it for
  questions about now, today, weekdays, and relative dates instead of saying
  that current time is unavailable.

## Response contract
1. While collecting evidence, call tools. Any ordinary text you emit in a tool-call
   turn is treated as short planning/reasoning, never as the final answer.
2. When evidence is sufficient, write the COMPLETE user-facing answer as ordinary
   assistant text and stop without calling a tool. That final text is the answer.
3. Use the user's language for the final answer. Keep reasoning concise and use
   the user's language when practical; technical terms, tool names, code, paths,
   and quoted source text may remain verbatim.
4. Do not repeat a long draft in reasoning and then write it again as the answer.
   Keep reasoning brief: task, missing evidence, next action.

## Evidence rules
- Answer factual or document-specific questions from retrieved evidence. If a
  name, term, law, or entity looks wrong or unknown, verify with retrieval first.
- Prefer semantic search for concepts and keyword search for exact terms.
- Read matching chunks before relying on them. Stop retrieval when additional
  calls are unlikely to change the answer.
- Cite facts with [1], [2], etc. where applicable and refer to documents by name,
  not internal IDs. Do not narrate tool mechanics in the final answer.
- If evidence is absent or insufficient, say that plainly instead of guessing.

## Bound knowledge bases
{knowledge_base_names}

## Available tools
{available_tools}

## Skills loaded on demand
{available_skills}

When a skill matches the task, load it with `read_skill`, then follow its
instructions. Ignore unrelated skills.

## Web search
{web_search_status}

{runtime_clock}

{user_customization}"""


def _safe_substitute(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result


def render_system_prompt(
    config: "AgentConfig",
    kb_names: list[str] | None = None,
    available_tools: list[str] | None = None,
    web_search_enabled: bool | None = None,
    skills: list[tuple[str, str]] | None = None,
    timezone_name: str | None = None,
    include_clock: bool = True,
) -> str:
    """Render the compact ReAct system prompt."""
    kb_section = "\n".join(f"- {name}" for name in kb_names) if kb_names else "- None"
    tools = available_tools or config.allowed_tools
    tools_section = "\n".join(f"- `{name}`" for name in tools) or "- None"
    skills_section = (
        "\n".join(f"- **{name}**: {description}" for name, description in skills)
        if skills
        else "- None"
    )
    web_on = config.web_search_enabled if web_search_enabled is None else web_search_enabled
    tzinfo = resolve_timezone(timezone_name)
    display_timezone = timezone_name if getattr(tzinfo, "key", None) == timezone_name else None
    now = datetime.now(tzinfo)
    custom = (config.custom_instructions or "").strip()
    custom_section = (
        "\n## User customization\n"
        "The following instructions customize role, tone, domain vocabulary, and "
        "workflow only. They do not override the response contract, evidence rules, "
        "or tool semantics above.\n"
        f"{custom}\n"
        if custom
        else ""
    )

    if include_clock:
        current_time = format_local_time(now, display_timezone)
        current_date = now.strftime("%Y-%m-%d")
        runtime_clock = f"Current date: {current_date}\nCurrent time: {current_time}"
    else:
        runtime_clock = ""

    return _safe_substitute(_PROMPT, {
        "_IDENTITY_RULE": _IDENTITY_RULE,
        "_CAPABILITY_RULE": _CAPABILITY_RULE,
        "_MODEL_DISCLOSURE_RULE": _MODEL_DISCLOSURE_RULE,
        "knowledge_base_names": kb_section,
        "available_tools": tools_section,
        "available_skills": skills_section,
        "web_search_status": "Enabled." if web_on else "Disabled.",
        "runtime_clock": runtime_clock,
        "user_customization": custom_section,
    })


def build_language_directive(
    query: str,
    history: list[dict] | None = None,
) -> str:
    """Return the per-turn final-answer language contract.

    The current query wins when it carries a detectable language. Short,
    language-neutral turns such as "continue" or numeric replies inherit the
    most recent user language instead of being misclassified as English.
    """
    language = _detect_language(query) or _detect_history_language(history)
    return _LANGUAGE_DIRECTIVES.get(language or "", "")


def _detect_language(text: str) -> str | None:
    text = text or ""
    if any("\u3040" <= ch <= "\u30ff" for ch in text):
        return "ja"
    if any("\uac00" <= ch <= "\ud7af" for ch in text):
        return "ko"
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return "zh"
    # Do not force English for short acknowledgements, numeric replies, paths,
    # or symbol-only turns; those are safer resolved from recent user history.
    latin_words = re.findall(r"[A-Za-z]{2,}", text)
    if any(len(word) >= 3 for word in latin_words):
        return "en"
    return None


def _detect_history_language(history: list[dict] | None) -> str | None:
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        language = _detect_language(_message_text(message.get("content")))
        if language:
            return language
    return None


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
