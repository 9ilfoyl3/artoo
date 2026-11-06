"""Structured LLM request failures and retry-safe classification."""

from __future__ import annotations

from dataclasses import dataclass
import re

_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


@dataclass(frozen=True)
class LlmFailureInfo:
    """Stable failure facts consumed by Agent retry and SSE rendering."""

    code: str
    message: str
    retryable: bool
    stage: str = "llm_call"
    status: int | None = None
    retry_after_ms: int | None = None

    @classmethod
    def from_exception(cls, exc: Exception) -> "LlmFailureInfo":
        message = str(exc) or type(exc).__name__
        declared_code = getattr(exc, "code", None)
        declared_stage = getattr(exc, "stage", None)
        declared_retryable = getattr(exc, "retryable", None)
        retry_after_ms = getattr(exc, "retry_after_ms", None)
        match = _HTTP_STATUS_RE.search(message)
        status = int(match.group(1)) if match else None

        if isinstance(declared_code, str) and declared_code:
            code = declared_code
        elif status == 429:
            code = "LLM_RATE_LIMIT"
        elif status in {500, 502, 503, 504}:
            code = "LLM_SERVER"
        elif status is not None:
            code = f"LLM_HTTP_{status}"
        elif any(token in message.lower() for token in ("timeout", "timed out", "stream stalled")):
            code = "LLM_TIMEOUT"
        elif "connect" in message.lower() or "transport" in message.lower():
            code = "LLM_TRANSPORT"
        else:
            code = "LLM_REQUEST_FAILED"

        retryable = (
            declared_retryable
            if isinstance(declared_retryable, bool)
            else status in _TRANSIENT_HTTP_CODES
            or code in {"LLM_TIMEOUT", "LLM_TRANSPORT", "LLM_RATE_LIMIT", "LLM_SERVER"}
        )
        return cls(
            code=code,
            message=message,
            retryable=retryable,
            stage=declared_stage if isinstance(declared_stage, str) and declared_stage else "llm_call",
            status=status,
            retry_after_ms=(
                retry_after_ms
                if isinstance(retry_after_ms, int) and retry_after_ms > 0
                else None
            ),
        )

    @property
    def user_message(self) -> str:
        if self.code == "LLM_STREAM_IDLE_TIMEOUT":
            return "模型响应超时，已停止等待。请重试。"
        if self.code == "LLM_TIMEOUT":
            return "模型请求超时，请重试。"
        if self.code == "LLM_RATE_LIMIT":
            return "模型服务当前繁忙，请稍后重试。"
        if self.code in {"LLM_SERVER", "LLM_TRANSPORT"}:
            return "模型服务暂时不可用，请稍后重试。"
        return "模型请求失败，请重试或调整问题。"


class LlmCallFailed(RuntimeError):
    """A model call exhausted recovery or failed outside the safe retry boundary."""

    def __init__(self, failure: LlmFailureInfo, *, attempts: int) -> None:
        self.failure = failure
        self.attempts = attempts
        super().__init__(
            f"{failure.code} after {attempts} attempt(s) during {failure.stage}: "
            f"{failure.message}"
        )
