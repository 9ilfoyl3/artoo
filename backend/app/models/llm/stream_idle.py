"""Provider transport idle watchdog for streaming LLM responses.

The timer belongs to the provider that owns the upstream transport. It is
armed only while waiting for the next raw stream item, so data consumed inside
the provider (including SSE comments and tool-call deltas that are not exposed
as typed chunks) still counts as activity.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from typing import TypeVar

DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
LLM_STREAM_IDLE_TIMEOUT_CODE = "LLM_STREAM_IDLE_TIMEOUT"

T = TypeVar("T")


class LlmStreamIdleTimeout(RuntimeError):
    """Raised when the provider transport produces no activity before its deadline."""

    code = LLM_STREAM_IDLE_TIMEOUT_CODE
    retryable = True

    def __init__(self, timeout_seconds: float, *, stage: str = "stream") -> None:
        self.timeout_seconds = timeout_seconds
        self.stage = stage
        super().__init__(
            f"LLM stream idle timeout after {timeout_seconds:g}s "
            f"during {stage}"
        )


def _validate_timeout(timeout_seconds: float) -> float:
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("stream_idle_timeout must be a positive finite number")
    return timeout


async def iter_with_stream_idle_timeout(
    iterator: AsyncIterator[T],
    timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    *,
    stage: str | Callable[[], str] = "stream",
) -> AsyncIterator[T]:
    """Yield upstream items while timing only outstanding transport reads.

    Each completed ``anext`` re-arms the full timeout for the next demand.
    Processing performed by the caller after a yield is outside the timer.
    """

    timeout = _validate_timeout(timeout_seconds)
    while True:
        try:
            async with asyncio.timeout(timeout):
                item = await anext(iterator)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            current_stage = stage() if callable(stage) else stage
            raise LlmStreamIdleTimeout(timeout, stage=current_stage) from exc
        yield item
