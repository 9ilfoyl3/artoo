"""BPE Token 估算器 - 基于 tiktoken 精确计数

使用 tiktoken 的 cl100k_base 编码进行 BPE token 计数，用于上下文窗口管理中的
增量（delta）估算与首轮全量估算。

token 计数的权威来源仍是 LLM API 返回的 usage 字段；本估算器仅在以下场景使用：
1. Delta 估算：每轮 LLM 调用后追加的新消息（assistant 回复 + tool 结果）的
   token 开销，避免额外的 API 往返。
2. 首轮 fallback：会话首轮没有历史 usage，可用本估算器做全量估算。

cl100k_base 仅为近似编码，不同模型族使用不同 tokenizer，因此对非 OpenAI 模型
计数不会完全精确。这是可接受的：估算只需足够接近以在大致正确的时机触发压缩，
小幅的高估/低估会在下一次 API 调用时被纠正。

IF tiktoken 编码器加载失败，则降级为字符数除以 4 的粗略估算（fallback 模式）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 每条消息的固定开销（role/content 包装等）
PER_MESSAGE_OVERHEAD = 3

# 对话尾部的固定开销（assistant 起始标记等）
PER_CONVERSATION_TAIL = 3

# 每个 tool_call 的固定开销
PER_TOOL_CALL_OVERHEAD = 4

# fallback 模式下的粗略估算比例：约 4 个字符 ≈ 1 token
_FALLBACK_CHARS_PER_TOKEN = 4

# BPE 编码名称，对应 GPT-3.5/GPT-4 系列
_ENCODING_NAME = "cl100k_base"


class TokenEstimator:
    """BPE Token 估算器

    使用 tiktoken 的 cl100k_base 编码进行 token 计数。若编码器加载失败，则
    降级为字符数除以 4 的粗略估算（fallback 模式），并记录 warning 日志。

    Attributes:
        is_fallback: 是否处于 fallback 模式（编码器加载失败时为 True）。
    """

    def __init__(self, encoding_name: str = _ENCODING_NAME) -> None:
        """初始化 BPE 编码器

        Args:
            encoding_name: tiktoken 编码名称，默认 cl100k_base。

        Note:
            IF tiktoken 编码器加载失败（如未安装或网络问题导致 BPE 词表无法
            加载），THEN 降级为字符数除以 4 的粗略估算并记录 warning 日志，
            同时将 is_fallback 置为 True。
        """
        self._encoder = None
        self.is_fallback = False
        self._calibration_ratio = 1.0

        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding(encoding_name)
        except Exception as exc:  # noqa: BLE001 - 任何加载失败都降级处理
            self.is_fallback = True
            logger.warning(
                "[TokenEstimator] tiktoken 编码器 '%s' 加载失败，降级为 "
                "字符数 / %d 的粗略估算：%s",
                encoding_name,
                _FALLBACK_CHARS_PER_TOKEN,
                exc,
            )

    def estimate_string(self, text: str) -> int:
        """估算单段文本的 token 数

        对文本做 BPE 编码并返回 token 数量。空字符串返回 0。
        fallback 模式下返回 len(text) // 4 的粗略估算。

        Args:
            text: 待估算的文本。

        Returns:
            token 数量；空字符串返回 0。
        """
        return self._calibrate(self._raw_estimate_string(text))

    def _raw_estimate_string(self, text: str) -> int:
        """Estimate without applying provider calibration."""
        if not text:
            return 0

        if self.is_fallback or self._encoder is None:
            return len(text) // _FALLBACK_CHARS_PER_TOKEN

        try:
            return len(self._encoder.encode(text))
        except Exception as exc:  # noqa: BLE001 - 编码异常时退回粗略估算
            logger.warning(
                "[TokenEstimator] BPE 编码失败，本次退回粗略估算：%s", exc
            )
            return len(text) // _FALLBACK_CHARS_PER_TOKEN

    def estimate_message(self, msg: dict) -> int:
        """估算单条消息的 token 数

        计算规则：
        - 每条消息固定开销 PER_MESSAGE_OVERHEAD(3)
        - 累加 role、content、name 字段的 token 数（缺失/None 视为空）
        - 对 tool_calls 中每个 tool_call，累加其 function.name 和
          function.arguments 的 token 数，外加每个 tool_call 固定开销
          PER_TOOL_CALL_OVERHEAD(4)

        Args:
            msg: 单条消息字典，可能包含 role/content/name/tool_calls 字段。

        Returns:
            该消息的估算 token 数。
        """
        return self._calibrate(self._raw_estimate_message(msg))

    def _raw_estimate_message(self, msg: dict) -> int:
        """Estimate one message without applying provider calibration."""
        tokens = PER_MESSAGE_OVERHEAD
        tokens += self._raw_estimate_string(msg.get("role") or "")
        tokens += self._raw_estimate_string(msg.get("content") or "")
        tokens += self._raw_estimate_string(msg.get("name") or "")

        for tool_call in msg.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            tokens += self._raw_estimate_string(function.get("name") or "")
            tokens += self._raw_estimate_string(function.get("arguments") or "")
            tokens += PER_TOOL_CALL_OVERHEAD

        return tokens

    def estimate_messages(self, messages: list[dict]) -> int:
        """估算消息列表的总 token 数

        累加所有消息的 token 数（estimate_message），再加上对话尾部固定开销
        PER_CONVERSATION_TAIL(3)。空列表返回 0（无消息时不计尾部开销）。

        Args:
            messages: 消息字典列表。

        Returns:
            消息列表的估算总 token 数；空列表返回 0。
        """
        return self._calibrate(self._raw_estimate_messages(messages))

    def _raw_estimate_messages(self, messages: list[dict]) -> int:
        """Estimate a message list without applying provider calibration."""
        if not messages:
            return 0

        total = sum(self._raw_estimate_message(msg) for msg in messages)
        total += PER_CONVERSATION_TAIL
        return total

    def _calibrate(self, raw_tokens: int) -> int:
        if raw_tokens <= 0:
            return 0
        return max(1, int(raw_tokens * self._calibration_ratio))

    def calibrate_from_usage(
        self,
        *,
        prompt_tokens: int,
        messages: list[dict],
        smoothing: float = 0.25,
    ) -> None:
        """Calibrate the estimator against authoritative provider usage.

        The ratio is bounded and smoothed to avoid letting one provider quirk
        destabilize context-window decisions. This is a runtime correction, not
        a model-specific tokenizer implementation.
        """
        raw = self._raw_estimate_messages(messages)
        if prompt_tokens <= 0 or raw <= 0:
            return
        observed = prompt_tokens / raw
        observed = max(0.5, min(2.0, observed))
        weight = max(0.0, min(1.0, float(smoothing)))
        self._calibration_ratio = (
            (1.0 - weight) * self._calibration_ratio + weight * observed
        )
