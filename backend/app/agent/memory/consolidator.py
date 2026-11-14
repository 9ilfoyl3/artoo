"""Memory Consolidation - LLM 摘要合并器

作为三层递进式上下文管理的中间层（介于 Token 估算与分组截断压缩之间）：当上下文
token 数超过 max_context_tokens * threshold（默认 50%）时，用 LLM 将早期历史消息摘要
为一段 [Memory Summary] 文本并合并进首条 system 消息，在保留关键信息的同时大幅减少
token 占用。

合并后的消息列表结构为：
    [system_msg_with_summary, *kept_history, *tail]

其中：
1. system_msg_with_summary：原始 system prompt（messages[0]）追加早期历史摘要。
2. kept_history：从 history 尾部按 token budget 保留的最近若干条历史，
   按 tool_call/tool_result 组不拆分。
3. tail：当前轮（最后一条 user 消息及其后续所有 assistant/tool 消息），
   始终完整保留。

LLM 调用使用与 Agent 相同的 LLMProvider.generate（async），temperature=0.3 以
确保摘要的事实准确性；最多重试 3 次，每次超时 60 秒；若全部失败则降级为纯文本
归档（raw archive）替代 LLM 摘要。
"""

from __future__ import annotations

import asyncio
import logging

from app.agent.memory.message_roles import normalize_system_messages
from app.agent.memory.token_estimator import TokenEstimator
from app.models.provider import LLMProvider

logger = logging.getLogger(__name__)

# 触发摘要合并的上下文窗口占用比例：超过 max_tokens * threshold 时触发。
DEFAULT_CONSOLIDATION_THRESHOLD = 0.5

# LLM 摘要调用的最大尝试次数，超过后降级为纯文本归档（raw archive）。
_MAX_CONSOLIDATION_ATTEMPTS = 3

# 单次 LLM 摘要调用的超时时间（秒）。
_CONSOLIDATION_TIMEOUT = 60.0

# 摘要调用使用的 temperature，低温度以确保事实准确性。
_CONSOLIDATION_TEMPERATURE = 0.3

# 摘要目标比例：瞄准 threshold 的 60%，为 summary + tail 预留空间。
_TARGET_RATIO = 0.6

# findKeepBoundary 中为 summary 消息预留的 token 数。
_SUMMARY_RESERVE_TOKENS = 500

# _build_consolidation_prompt 中各角色消息内容的截断长度（字符）。
_PROMPT_TRUNCATE_USER = 2000
_PROMPT_TRUNCATE_ASSISTANT = 1000
_PROMPT_TRUNCATE_TOOL = 1000

# _raw_archive 中每条消息内容的截断长度（字符）。
_ARCHIVE_TRUNCATE = 500

# 摘要系统提示词：指示 LLM 做简洁、保真的对话摘要。
CONSOLIDATION_SYSTEM_PROMPT = (
    "You are a conversation summarizer. "
    "Your task is to create a concise but comprehensive summary "
    "of a conversation between a user and an AI assistant.\n\n"
    "The summary should:\n"
    "- Be written in the same language as the original conversation\n"
    "- Preserve all key facts, numbers, and specific details\n"
    "- Include the outcomes of any tool executions\n"
    "- Note any errors or issues encountered\n"
    "- Be structured with clear sections if the conversation covered multiple topics\n"
    "- Be concise — aim for 30% or less of the original length\n\n"
    "Output only the summary, no preamble or explanation."
)


class MemoryConsolidator:
    """LLM 摘要合并器

    在上下文利用率超过阈值（默认 50%）时，将早期历史消息用 LLM 摘要为一段
    [Memory Summary] 文本并合并进首条 system 消息，保留 system prompt + 摘要 +
    最近历史 + 当前轮。

    LLM 调用复用 Agent 的 LLMProvider 实例（generate 为 async），并通过传入的
    TokenEstimator 计算 token budget 以确定保留边界。

    Attributes:
        无公开属性，构造参数均以私有字段持有。
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        estimator: TokenEstimator,
        max_context_tokens: int,
        threshold: float = DEFAULT_CONSOLIDATION_THRESHOLD,
    ) -> None:
        """初始化摘要合并器

        Args:
            llm_provider: LLM Provider 实例，需提供 async generate(messages, **kwargs)。
            estimator: TokenEstimator 实例，用于估算消息 token 数。
            max_context_tokens: 最大上下文窗口 token 数。
            threshold: 触发摘要的占用比例（0-1，默认 0.5）。
                超出 (0, 1) 范围时回退为默认值 0.5。
        """
        if threshold <= 0 or threshold >= 1:
            threshold = DEFAULT_CONSOLIDATION_THRESHOLD

        self._llm_provider = llm_provider
        self._estimator = estimator
        self._max_tokens = max_context_tokens
        self._threshold = threshold

    def should_consolidate(self, current_tokens: int) -> bool:
        """判断是否需要触发摘要合并

        当 current_tokens 超过 max_tokens * threshold 时返回 True。
        current_tokens 应优先来自 LLM API 返回的 usage（见 UsageTracker），
        无 usage 时用 BPE 全量估算。

        Args:
            current_tokens: 调用方对当前上下文 token 数的最佳估算。

        Returns:
            需要触发摘要时返回 True；max_tokens <= 0 时始终返回 False。
        """
        if self._max_tokens <= 0:
            return False
        trigger_at = int(self._max_tokens * self._threshold)
        return current_tokens > trigger_at

    async def consolidate(self, messages: list[dict]) -> list[dict]:
        """将早期历史消息摘要合并进首条 system 并返回压缩后的消息列表

        分区与保留规则：
        - system_msg：messages[0]，始终保留并承载早期历史摘要。
        - tail：从最后一条 user 消息到列表末尾（当前轮），始终完整保留。
        - history：system 与 tail 之间的中间部分。从尾部按 token budget 计算
          keep_from_end 条保留（_find_keep_boundary，组不拆分），其余早期消息
          （to_consolidate）用 LLM 摘要为一段 [Memory Summary] 文本并合并进首条 system。

        最终结构：[system_msg_with_summary, *to_keep, *tail]。

        Args:
            messages: 消息字典列表，messages[0] 约定为 system prompt。

        Returns:
            压缩后的消息列表。以下情况原样返回不做合并：
            - 消息列表仅有 3 条或更少（Req 4.7）；
            - 找不到有效的当前轮 user 消息（last_user_idx <= 1）；
            - history 不足 2 条；
            - 按 budget 计算需保留全部 history（无可摘要的早期消息）。
        """
        # Req 4.7：消息列表仅有 3 条或更少时原样返回。
        if len(messages) <= 3:
            return messages

        system_msg = messages[0]

        # 定位当前轮 user 查询：从尾部向前找最后一条 role == "user" 的消息。
        # 从该消息到末尾即为当前轮（tail），须完整保留。
        last_user_idx = 0
        for i in range(len(messages) - 1, 0, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        # 当前轮紧贴 system，无中间历史可摘要。
        if last_user_idx <= 1:
            return messages

        history = messages[1:last_user_idx]
        tail = messages[last_user_idx:]

        # history 太短不值得摘要。
        if len(history) < 2:
            return messages

        # 摘要目标 token 数：瞄准 threshold 的 60%，为 summary + tail 预留空间。
        target_tokens = int(self._max_tokens * self._threshold * _TARGET_RATIO)

        tail_tokens = sum(self._estimator.estimate_message(msg) for msg in tail)

        keep_from_end = self._find_keep_boundary(
            history, target_tokens, system_msg, tail_tokens
        )

        # budget 足以容纳全部 history，无早期消息需要摘要，幂等返回。
        if keep_from_end >= len(history):
            return messages

        to_consolidate = history[: len(history) - keep_from_end]
        to_keep = history[len(history) - keep_from_end :]

        # LLM 摘要，失败时降级为纯文本归档。
        try:
            summary = await self._summarize_with_retry(to_consolidate)
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级为 raw archive
            logger.warning(
                "[MemoryConsolidator] LLM 摘要重试均失败，降级为纯文本归档：%s",
                exc,
            )
            summary = self._raw_archive(to_consolidate)

        summary_msg = {
            "role": "system",
            "content": (
                f"[Memory Summary - {len(to_consolidate)} earlier messages "
                f"consolidated]\n\n{summary}"
            ),
        }

        # OpenAI-compatible providers require the system role to appear only at
        # the beginning. Keep the summary in the existing system prompt rather
        # than introducing a second system message mid-conversation.
        result: list[dict] = [system_msg, summary_msg]
        result.extend(to_keep)
        result.extend(tail)
        result = normalize_system_messages(result)

        logger.info(
            "[MemoryConsolidator] 摘要合并完成：%d 条早期消息 -> 摘要（%d 字符），"
            "保留 %d 条历史 + %d 条当前轮消息（%d 条 -> %d 条）",
            len(to_consolidate),
            len(summary),
            len(to_keep),
            len(tail),
            len(messages),
            len(result),
        )

        return result

    def _find_keep_boundary(
        self,
        history: list[dict],
        target_tokens: int,
        system_msg: dict,
        tail_tokens: int,
    ) -> int:
        """从 history 尾部计算需保留的消息条数

        从 history 最后一条开始向前累加 token，直到超出 budget，返回应保留的
        消息条数（从尾部计）。budget 在 target_tokens 基础上扣除 system_msg、
        tail 以及为 summary 消息预留的 _SUMMARY_RESERVE_TOKENS(500)。

        分组规则：遇到 role == "tool" 的消息时，
        向前合并其连续的 tool 消息以及紧邻的 assistant 消息为一组，组内 token
        作为整体计算，保证 tool_call/tool_result 配对不被拆分。

        Args:
            history: 中间历史消息列表。
            target_tokens: 摘要目标 token 数。
            system_msg: system prompt 消息（用于扣除其 token 开销）。
            tail_tokens: 当前轮 tail 的 token 总数（始终保留，需扣除）。

        Returns:
            从 history 尾部应保留的消息条数；budget <= 0 时返回 0。
        """
        budget = (
            target_tokens
            - self._estimator.estimate_message(system_msg)
            - tail_tokens
            - _SUMMARY_RESERVE_TOKENS
        )

        if budget <= 0:
            return 0

        tokens = 0
        keep_count = 0
        i = len(history) - 1

        while i >= 0:
            msg = history[i]
            msg_tokens = self._estimator.estimate_message(msg)

            if msg.get("role") == "tool":
                # 向前合并连续 tool 消息 + 紧邻的 assistant 消息为一组。
                group_tokens = msg_tokens
                group_size = 1
                j = i - 1
                while j >= 0 and history[j].get("role") == "tool":
                    group_tokens += self._estimator.estimate_message(history[j])
                    group_size += 1
                    j -= 1
                if j >= 0 and history[j].get("role") == "assistant":
                    group_tokens += self._estimator.estimate_message(history[j])
                    group_size += 1

                if tokens + group_tokens > budget:
                    break
                tokens += group_tokens
                keep_count += group_size
                i -= group_size
            else:
                if tokens + msg_tokens > budget:
                    break
                tokens += msg_tokens
                keep_count += 1
                i -= 1

        return keep_count

    async def _summarize_with_retry(self, messages: list[dict]) -> str:
        """构建摘要 prompt 并调用 LLM，含重试与超时

        最多重试 _MAX_CONSOLIDATION_ATTEMPTS(3) 次，每次调用通过 asyncio.wait_for
        限制 _CONSOLIDATION_TIMEOUT(60) 秒超时，temperature=0.3。任一次返回非空
        内容即成功；全部失败则抛出 RuntimeError（由 consolidate 捕获后降级为
        raw archive）。

        Args:
            messages: 待摘要的早期历史消息列表。

        Returns:
            LLM 生成的摘要文本。

        Raises:
            RuntimeError: 所有重试均失败（超时、异常或返回空内容）。
        """
        prompt = self._build_consolidation_prompt(messages)
        summary_messages = [
            {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        last_err: Exception | None = None

        for attempt in range(1, _MAX_CONSOLIDATION_ATTEMPTS + 1):
            try:
                content = await asyncio.wait_for(
                    self._llm_provider.generate(
                        summary_messages,
                        temperature=_CONSOLIDATION_TEMPERATURE,
                    ),
                    timeout=_CONSOLIDATION_TIMEOUT,
                )
                if content and content.strip():
                    return content
                last_err = RuntimeError("LLM 返回空摘要内容")
            except asyncio.TimeoutError as exc:
                last_err = exc
                logger.warning(
                    "[MemoryConsolidator] 摘要调用第 %d/%d 次超时（%.0fs）",
                    attempt,
                    _MAX_CONSOLIDATION_ATTEMPTS,
                    _CONSOLIDATION_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 - 重试所有调用异常
                last_err = exc
                logger.warning(
                    "[MemoryConsolidator] 摘要调用第 %d/%d 次失败：%s",
                    attempt,
                    _MAX_CONSOLIDATION_ATTEMPTS,
                    exc,
                )

        raise RuntimeError(
            f"摘要在 {_MAX_CONSOLIDATION_ATTEMPTS} 次尝试后仍失败：{last_err}"
        )

    def _build_consolidation_prompt(self, messages: list[dict]) -> str:
        """将待摘要消息格式化为 LLM 摘要 prompt

        prompt 头部列出摘要时需保留的要点（关键事实/工具结果/用户意图/错误信息），
        随后逐条格式化消息内容并按角色截断（user 2000、assistant 1000、tool 1000
        字符）。assistant 含 tool_calls 时标注调用的工具名。

        Args:
            messages: 待摘要的消息列表。

        Returns:
            完整的摘要 prompt 字符串。
        """
        parts: list[str] = [
            "Summarize the following conversation history, preserving:\n",
            "1. Key facts and decisions made\n",
            "2. Tool execution results and their outcomes\n",
            "3. User's original intent and requirements\n",
            "4. Any errors encountered and how they were resolved\n\n",
            "Conversation to summarize:\n\n",
        ]

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content") or ""

            if role == "user":
                parts.append(
                    f"**User**: {self._truncate(content, _PROMPT_TRUNCATE_USER)}\n\n"
                )
            elif role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                truncated = self._truncate(content, _PROMPT_TRUNCATE_ASSISTANT)
                if tool_calls:
                    names = self._tool_call_names(tool_calls)
                    parts.append(
                        f"**Assistant** [called tools: {', '.join(names)}]: "
                        f"{truncated}\n\n"
                    )
                else:
                    parts.append(f"**Assistant**: {truncated}\n\n")
            elif role == "tool":
                name = msg.get("name") or ""
                parts.append(
                    f"**Tool [{name}]**: "
                    f"{self._truncate(content, _PROMPT_TRUNCATE_TOOL)}\n\n"
                )

        return "".join(parts)

    def _raw_archive(self, messages: list[dict]) -> str:
        """LLM 摘要失败时的 fallback：纯文本 dump 每条消息

        逐条 dump 消息内容（每条截断 _ARCHIVE_TRUNCATE(500) 字符），保留对话的
        原始信息以替代 LLM 摘要。assistant 含 tool_calls 时标注工具名。

        Args:
            messages: 待归档的消息列表。

        Returns:
            纯文本归档字符串。
        """
        parts: list[str] = [
            "Raw conversation archive (LLM summarization unavailable):\n\n"
        ]

        for msg in messages:
            role = msg.get("role")
            content = self._truncate(msg.get("content") or "", _ARCHIVE_TRUNCATE)

            if role == "user":
                parts.append(f"- User: {content}\n")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    names = self._tool_call_names(tool_calls)
                    parts.append(f"- Assistant [tools: {','.join(names)}]: {content}\n")
                else:
                    parts.append(f"- Assistant: {content}\n")
            elif role == "tool":
                name = msg.get("name") or ""
                parts.append(f"- Tool[{name}]: {content}\n")

        return "".join(parts)

    @staticmethod
    def _tool_call_names(tool_calls: list[dict]) -> list[str]:
        """从 tool_calls 列表中提取每个 function.name

        Args:
            tool_calls: OpenAI 格式的 tool_calls 列表。

        Returns:
            工具名称列表（缺失 name 的以空字符串占位）。
        """
        names: list[str] = []
        for tc in tool_calls:
            function = tc.get("function") or {}
            names.append(function.get("name") or "")
        return names

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """将文本截断至 max_len 字符，超出时追加省略号

        Args:
            text: 待截断文本。
            max_len: 最大保留字符数。

        Returns:
            截断后的文本；未超长时原样返回。
        """
        if not text or len(text) <= max_len:
            return text
        return text[:max_len] + "..."
