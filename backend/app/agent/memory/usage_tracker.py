"""API Usage 追踪器 - 结合 API Usage 与 BPE Delta 估算

token 计数的权威来源是 LLM API 返回的 usage 字段（prompt_tokens /
completion_tokens / total_tokens）。
本追踪器记录上一次 API 调用返回的 usage 以及当时发送的消息数量，从而在下一轮
估算当前上下文 token 数时，只对「新增消息」做 BPE 估算（delta 估算），避免每轮
都对全部历史消息做全量 BPE 估算。

估算策略（见 estimate_current_tokens）：
1. Delta 估算：有历史 usage（total_tokens > 0）且消息数增长时，使用
   last_total_tokens + estimator.estimate_messages(新增消息) 作为估算值。
2. 全量 fallback：无历史 usage 或消息数未增长时，对全部消息做全量 BPE 估算。

IF LLM API 未返回 usage 字段（或 total_tokens <= 0），THEN 不更新内部记录，
下次估算继续使用全量 BPE，直到拿到一次有效 usage 为止。
"""

from __future__ import annotations

import logging

from app.agent.memory.token_estimator import TokenEstimator
from app.models.provider import TokenUsage

logger = logging.getLogger(__name__)


class UsageTracker:
    """API Usage 追踪器

    记录上一次 LLM API 调用返回的 usage 与发送消息数量，用于在估算当前上下文
    token 数时做增量（delta）估算，减少全量 BPE 估算的开销。

    Attributes:
        _estimator: BPE Token 估算器，用于对新增消息或全量消息做估算。
        _last_usage: 上一次有效的 API usage 记录（total_tokens > 0）；尚无
            记录时为 None。
        _last_sent_msg_count: 上一次 API 调用时发送的消息数量；尚无记录时为 0。
    """

    def __init__(self, estimator: TokenEstimator) -> None:
        """初始化追踪器

        Args:
            estimator: BPE Token 估算器实例，由调用方注入。
        """
        self._estimator = estimator
        self._last_usage: TokenUsage | None = None
        self._last_sent_msg_count: int = 0

    def update(
        self,
        usage: TokenUsage,
        sent_msg_count: int,
        messages: list[dict] | None = None,
    ) -> None:
        """记录一次 API 调用的 usage

        WHEN LLM API 返回 usage 且 total_tokens > 0，记录该 usage 和本次发送的
        消息数量，供下一轮 delta 估算使用。

        IF usage 为空或 total_tokens <= 0，THEN 不更新内部记录（下次估算继续
        使用全量 BPE）。

        Args:
            usage: API 返回的 token 用量统计。
            sent_msg_count: 本次 API 调用时发送的消息数量（即对应该 usage 的
                prompt 消息条数）。
        """
        if usage is None or usage.total_tokens <= 0:
            return

        self._last_usage = usage
        self._last_sent_msg_count = sent_msg_count
        if messages is not None and usage.prompt_tokens > 0:
            self._estimator.calibrate_from_usage(
                prompt_tokens=usage.prompt_tokens,
                messages=messages,
            )

    def estimate_current_tokens(self, messages: list[dict]) -> int:
        """估算当前上下文的总 token 数

        优先使用 delta 估算：有历史 usage（total_tokens > 0）且消息数相比上次
        发送时有增长时，返回 last_total_tokens + estimator.estimate_messages(
        新增消息)，其中「新增消息」为 messages[_last_sent_msg_count:]。

        IF 无历史 usage 记录或消息数未增长，THEN fallback 到对全部消息做全量
        BPE 估算 estimator.estimate_messages(messages)。

        Args:
            messages: 当前完整的消息列表。

        Returns:
            当前上下文的估算 token 数。
        """
        if (
            self._last_usage is not None
            and self._last_usage.total_tokens > 0
            and len(messages) > self._last_sent_msg_count
        ):
            new_messages = messages[self._last_sent_msg_count:]
            delta = self._estimator.estimate_messages(new_messages)
            return self._last_usage.total_tokens + delta

        return self._estimator.estimate_messages(messages)
