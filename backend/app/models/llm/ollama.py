"""Ollama LLM 实现

通过 httpx 异步客户端调用 Ollama 的 /api/chat 接口，
支持同步生成、流式生成、以及 Function Calling 模式。
"""

import json
from typing import AsyncIterator
from uuid import uuid4

import httpx

from app.models.llm.stream_idle import (
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    iter_with_stream_idle_timeout,
)
from app.models.provider import (
    ChatResponse,
    LLMProvider,
    LLMToolCall,
    StreamChunk,
    TokenUsage,
)


class OllamaLLM(LLMProvider):
    """Ollama LLM Provider，基于 httpx 异步调用"""

    def __init__(self, base_url: str, model: str, max_output_tokens: int | None = None):
        """初始化 Ollama 客户端

        Args:
            base_url: Ollama 服务地址，如 http://localhost:11434
            model: 模型名称，如 qwen2.5:7b
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    def _build_payload(self, messages: list[dict], stream: bool, **kwargs) -> dict:
        """统一构造 Ollama /api/chat 请求体，翻译通用参数为 Ollama 原生格式。

        Ollama 不认 OpenAI 风格的顶层 enable_thinking / temperature：
        - enable_thinking（前端唯一思考开关）→ Ollama 原生布尔字段 `think`
        - temperature 等采样参数 → 归入 `options` 对象
        其余 kwargs 透传。这样思考开关对 Qwen3/DeepSeek 等 Ollama 推理模型真正生效。
        """
        enable_thinking = kwargs.pop("enable_thinking", None)
        temperature = kwargs.pop("temperature", None)
        max_tokens = kwargs.pop("max_tokens", self.max_output_tokens)

        options = kwargs.pop("options", None)
        if not isinstance(options, dict):
            options = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }
        if options:
            payload["options"] = options
        if enable_thinking is not None:
            payload["think"] = bool(enable_thinking)
        return payload

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成完整回复

        Args:
            messages: 对话消息列表，格式 [{"role": "user", "content": "..."}]

        Returns:
            模型生成的完整文本
        """
        payload = self._build_payload(messages, stream=False, **kwargs)
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Ollama 响应格式异常: {e}") from e

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成回复，逐块返回内容

        Args:
            messages: 对话消息列表

        Yields:
            模型生成的文本片段
        """
        stream_idle_timeout = float(kwargs.pop(
            "stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        ))
        payload = self._build_payload(messages, stream=True, **kwargs)
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=payload, timeout=stream_idle_timeout
            ) as resp:
                resp.raise_for_status()
                # Ollama 流式返回 NDJSON，每行一个 JSON 对象
                lines = resp.aiter_lines().__aiter__()
                async for line in iter_with_stream_idle_timeout(
                    lines,
                    stream_idle_timeout,
                    stage="content",
                ):
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Ollama 流式请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> ChatResponse:
        """Function Calling: 非流式调用 Ollama /api/chat 并解析 tool_calls

        Args:
            messages: 对话消息列表
            tools: OpenAI 格式的工具定义列表

        Returns:
            ChatResponse 包含 content、tool_calls、finish_reason、usage
        """
        payload = self._build_payload(messages, stream=False, tools=tools, **kwargs)
        if not tools:
            payload.pop("tools", None)
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

            message = data.get("message", {})
            content = message.get("content", "") or ""
            reasoning_content = message.get("thinking") or message.get("reasoning") or ""
            raw_tool_calls = message.get("tool_calls", []) or []

            # 解析 tool_calls：Ollama 返回 arguments 为 dict，需要 json.dumps
            tool_calls = self._parse_tool_calls(raw_tool_calls)

            # 确定 finish_reason
            finish_reason = "tool_calls" if tool_calls else "stop"

            # 解析 token 用量
            usage = self._parse_usage(data)

            return ChatResponse(
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Ollama Function Calling 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Ollama 响应格式异常: {e}") from e

    async def stream_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """Streaming Function Calling: 流式调用 Ollama /api/chat

        Ollama 流式模式下，tool_calls 出现在最终 chunk 的 message 中（非增量）。
        普通 content 逐块返回，tool_calls 在 done=true 的 chunk 中一次性返回。

        Args:
            messages: 对话消息列表
            tools: OpenAI 格式的工具定义列表

        Yields:
            StreamChunk 包含 content 或 tool_calls
        """
        stream_idle_timeout = float(kwargs.pop(
            "stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        ))
        payload = self._build_payload(messages, stream=True, tools=tools, **kwargs)
        if not tools:
            payload.pop("tools", None)
        stage = "waiting_first_event"
        try:
            async with self._client.stream(
                "POST", "/api/chat", json=payload, timeout=stream_idle_timeout
            ) as resp:
                resp.raise_for_status()
                lines = resp.aiter_lines().__aiter__()
                async for line in iter_with_stream_idle_timeout(
                    lines,
                    stream_idle_timeout,
                    stage=lambda: stage,
                ):
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    message = chunk.get("message", {})
                    is_done = chunk.get("done", False)

                    # 处理 content（逐块流式返回）
                    content = message.get("content", "") or ""
                    if content:
                        stage = "content"
                        yield StreamChunk(
                            content=content,
                            response_type="content",
                        )

                    # Qwen/DeepSeek 推理模型可能把 thinking 放在独立通道。
                    thinking = message.get("thinking") or message.get("reasoning") or ""
                    if thinking:
                        stage = "reasoning"
                        yield StreamChunk(
                            reasoning=thinking,
                            response_type="thinking",
                        )

                    # 处理 tool_calls（Ollama 在最终 chunk 中一次性返回）
                    raw_tool_calls = message.get("tool_calls", []) or []
                    if raw_tool_calls:
                        stage = "finishing"
                        tool_calls = self._parse_tool_calls(raw_tool_calls)
                        yield StreamChunk(
                            tool_calls=tool_calls,
                            finish_reason="tool_calls",
                            response_type="tool_call",
                        )

                    # 流结束标记
                    if is_done:
                        stage = "finishing"
                        raw_usage = self._parse_usage(chunk)
                        if raw_usage:
                            yield StreamChunk(usage=raw_usage, response_type="usage")
                        # 如果没有 tool_calls，发送 stop finish_reason
                        if not raw_tool_calls:
                            yield StreamChunk(
                                finish_reason="stop",
                            )
                        break
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Ollama 流式 Function Calling 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Ollama 连接失败: {e}") from e

    def _parse_tool_calls(self, raw_tool_calls: list) -> list[LLMToolCall]:
        """解析 Ollama 格式的 tool_calls 为 LLMToolCall 列表

        Ollama tool_call 格式: {"function": {"name": "...", "arguments": {...}}}
        注意: Ollama 返回 arguments 为 dict，需要 json.dumps 转为字符串。
        Ollama 不提供 tool_call id，需要自行生成。
        """
        tool_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            arguments = func.get("arguments", {})

            # Ollama 返回 arguments 为 dict，转为 JSON 字符串
            if isinstance(arguments, dict):
                args_str = json.dumps(arguments, ensure_ascii=False)
            else:
                args_str = str(arguments)

            tool_calls.append(
                LLMToolCall(
                    id=f"call_{uuid4().hex[:8]}",
                    function_name=name,
                    arguments=args_str,
                )
            )
        return tool_calls

    def _parse_usage(self, data: dict) -> TokenUsage | None:
        """从 Ollama 响应中解析 token 用量

        Ollama 使用 prompt_eval_count 和 eval_count 字段。
        """
        prompt_tokens = data.get("prompt_eval_count", 0) or 0
        completion_tokens = data.get("eval_count", 0) or 0
        if prompt_tokens or completion_tokens:
            return TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
        return None

    async def close(self):
        """关闭 HTTP 客户端连接"""
        await self._client.aclose()
