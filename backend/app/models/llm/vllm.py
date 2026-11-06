"""vLLM LLM 实现

通过 httpx 异步客户端调用 vLLM 的 OpenAI 兼容 API，
支持同步生成、流式生成、Function Calling 三种模式。
"""

import json
from typing import AsyncIterator

import httpx

from app.models.llm.provider_detect import LLMProviderName, detect_provider
from app.models.llm.stream_idle import (
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    iter_with_stream_idle_timeout,
)
from app.models.llm.thinking_dialect import apply_thinking
from app.models.provider import (
    ChatResponse,
    LLMProvider,
    LLMToolCall,
    StreamChunk,
    TokenUsage,
)


class VllmLLM(LLMProvider):
    """vLLM LLM Provider，基于 OpenAI 兼容接口"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        provider: str | None = None,
        thinking_control: str | None = None,
        max_output_tokens: int | None = None,
    ):
        """初始化 vLLM 客户端

        Args:
            base_url: API 基础地址，代码会自动拼接 /chat/completions
                      如 http://localhost:8000/v1
                      或 https://ark.cn-beijing.volces.com/api/coding/v3
            model: 模型名称
            api_key: API 密钥（远端服务需要）
            provider: 可选，显式指定模型厂商（thinking 方言分派用）。
                      为空时根据 base_url 自动检测。前端无需配置——仅在需要覆盖
                      自动检测时使用。
            thinking_control: 可选，显式指定「思考模式参数格式」
                      （none / chat_template_kwargs / enable_thinking / thinking_type）。
                      给定时优先级最高，直接决定 thinking 开关写入哪种字段，
                      跳过厂商/模型名自动猜测（取代脆弱的自动匹配）。
            max_output_tokens: 模型级默认单次输出上限；请求显式 max_tokens 优先。
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.thinking_control = thinking_control or None
        self.max_output_tokens = max_output_tokens
        # 模型厂商：决定 thinking 开关注入到请求体的哪个字段（见 thinking_dialect）。
        # 显式指定优先；否则按 base_url 自动检测。
        if provider:
            try:
                self.provider = LLMProviderName(provider)
            except ValueError:
                self.provider = detect_provider(self.base_url, self.model)
        else:
            self.provider = detect_provider(self.base_url, self.model)
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        print(
            f"[vLLM] 初始化 - base_url: {self.base_url}, model: {self.model}, "
            f"provider: {self.provider.value}, api_key 长度: {len(api_key) if api_key else 0}"
        )
        self._client = httpx.AsyncClient(timeout=120.0, headers=headers)

    def _build_payload(self, messages: list[dict], stream: bool, **kwargs) -> dict:
        """统一构造请求体，并按厂商方言注入 thinking 开关。

        从 kwargs 中拦截 enable_thinking（前端唯一的思考开关），转交 thinking_dialect
        按厂商写入正确的字段（chat_template_kwargs / 顶层 enable_thinking / thinking.type
        等），而非盲目拼进顶层 payload。其余 kwargs（temperature、tool_choice 等）透传。
        """
        enable_thinking = kwargs.pop("enable_thinking", None)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            **kwargs,
        }
        if self.max_output_tokens is not None and payload.get("max_tokens") is None:
            payload["max_tokens"] = self.max_output_tokens
        payload = apply_thinking(
            payload,
            enable_thinking,
            self.base_url,
            self.model,
            self.provider,
            self.thinking_control,
        )
        return payload

    async def generate(self, messages: list[dict], **kwargs) -> str:
        """同步生成完整回复

        Args:
            messages: 对话消息列表，格式 [{"role": "user", "content": "..."}]

        Returns:
            模型生成的完整文本
        """
        request_timeout = kwargs.pop(
            "_request_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        )
        payload = self._build_payload(messages, stream=False, **kwargs)
        try:
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] 请求 URL: {url}")
            print(f"[vLLM] 请求 payload keys: {list(payload.keys())}, chat_template_kwargs={payload.get('chat_template_kwargs', 'not set')}")
            print(f"[vLLM] Client Headers: {dict(self._client.headers)}")
            resp = await self._client.post(url, json=payload, timeout=request_timeout)
            print(f"[vLLM] 实际请求 Headers: {dict(resp.request.headers)}")
            print(f"[vLLM] 响应状态码: {resp.status_code}")
            print(f"[vLLM] 响应 Headers: {resp.headers}")
            print(f"[vLLM] 响应内容: {resp.text[:1000]}")
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"vLLM 请求失败: HTTP {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e
        except (KeyError, TypeError, IndexError) as e:
            raise RuntimeError(f"vLLM 响应格式异常: {e}") from e

    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]:
        """流式生成回复，逐块返回内容

        如果远端不支持流式（返回非 200），自动降级为非流式调用并逐段输出。

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
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] 流式请求 URL: {url}")
            async with self._client.stream(
                "POST", url, json=payload, timeout=stream_idle_timeout
            ) as resp:
                if resp.status_code != 200:
                    # 流式不支持，降级为非流式
                    print(f"[vLLM] 流式请求返回 {resp.status_code}，降级为非流式")
                    await resp.aclose()
                    result = await self.generate(
                        messages, _request_timeout=stream_idle_timeout, **kwargs
                    )
                    # 分段输出模拟流式
                    chunk_size = 4
                    for i in range(0, len(result), chunk_size):
                        yield result[i:i + chunk_size]
                    return
                # 正常流式处理：vLLM 流式返回 SSE 格式 data: {...}\n\n
                lines = resp.aiter_lines().__aiter__()
                async for line in iter_with_stream_idle_timeout(
                    lines,
                    stream_idle_timeout,
                    stage="content",
                ):
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    content = choices[0].get("delta", {}).get("content", "")
                    if content:
                        yield content
        except httpx.HTTPStatusError as e:
            # raise_for_status 触发的异常，同样降级
            print(f"[vLLM] 流式 HTTPStatusError {e.response.status_code}，降级为非流式")
            result = await self.generate(
                messages, _request_timeout=stream_idle_timeout, **kwargs
            )
            chunk_size = 4
            for i in range(0, len(result), chunk_size):
                yield result[i:i + chunk_size]
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> ChatResponse:
        """Function Calling: 发送消息和工具定义，获取包含 tool_calls 的响应

        通过 OpenAI 兼容 API 的 tools 参数发送工具定义，
        解析 response.choices[0].message.tool_calls。

        Args:
            messages: 对话消息列表
            tools: OpenAI 格式的工具定义列表
            **kwargs: 额外参数（temperature, tool_choice 等）

        Returns:
            ChatResponse 包含 content、tool_calls、finish_reason、usage
        """
        request_timeout = kwargs.pop(
            "_request_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        )
        payload = self._build_payload(messages, stream=False, tools=tools, **kwargs)
        # 如果 tools 为空列表，不发送 tools 参数（某些 API 不接受空 tools）
        if not tools:
            payload.pop("tools", None)
        try:
            url = f"{self.base_url}/chat/completions"
            print(f"[vLLM] chat_with_tools: {len(tools)} tools, url={url}")
            print(f"[vLLM] chat_with_tools payload keys: {list(payload.keys())}")
            resp = await self._client.post(url, json=payload, timeout=request_timeout)
            if resp.status_code != 200:
                print(
                    f"[vLLM] chat_with_tools 失败 状态码: {resp.status_code}, "
                    f"响应内容: {resp.text[:1000]}"
                )
            resp.raise_for_status()
            data = resp.json()

            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # 解析 content
            content = message.get("content") or ""
            reasoning_content = (
                message.get("reasoning_content")
                or message.get("reasoning")
                or ""
            )

            # 解析 tool_calls
            tool_calls: list[LLMToolCall] = []
            raw_tool_calls = message.get("tool_calls")
            if raw_tool_calls:
                for tc in raw_tool_calls:
                    tool_calls.append(
                        LLMToolCall(
                            id=tc["id"],
                            function_name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        )
                    )

            # 解析 usage
            usage = None
            raw_usage = data.get("usage")
            if raw_usage:
                usage = TokenUsage(
                    prompt_tokens=raw_usage.get("prompt_tokens", 0),
                    completion_tokens=raw_usage.get("completion_tokens", 0),
                    total_tokens=raw_usage.get("total_tokens", 0),
                )

            return ChatResponse(
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"vLLM chat_with_tools 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e
        except (KeyError, TypeError, IndexError) as e:
            raise RuntimeError(f"vLLM 响应格式异常: {e}") from e

    async def stream_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs
    ) -> AsyncIterator[StreamChunk]:
        """OpenAI 兼容响应转换为类型化 StreamChunk。

        provider 只负责通道分类：content 是 content，reasoning_content/reasoning
        是 reasoning。tool-call arguments 由 provider 组装成完整调用；是否属于
        thinking 还是用户正文由 Agent loop 根据 finish reason 和 tool calls 判定。
        """
        stream_idle_timeout = float(kwargs.pop(
            "stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        ))
        payload = self._build_payload(messages, stream=True, tools=tools, **kwargs)
        if "stream_options" not in payload:
            payload["stream_options"] = {"include_usage": True}

        # 累积 tool_calls 的状态（按 index 存储）
        tool_call_map: dict[int, dict] = {}
        actual_finish_reason = ""
        finish_emitted = False
        stage = "waiting_first_event"

        try:
            url = f"{self.base_url}/chat/completions"
            async with self._client.stream(
                "POST", url, json=payload, timeout=stream_idle_timeout
            ) as resp:
                if resp.status_code != 200:
                    # 流式不支持，降级为非流式
                    await resp.aclose()
                    result = await self.chat_with_tools(
                        messages,
                        tools,
                        _request_timeout=stream_idle_timeout,
                        **kwargs,
                    )
                    # 将非流式结果转为单个 StreamChunk 输出
                    if result.reasoning_content:
                        yield StreamChunk(
                            reasoning=result.reasoning_content,
                            response_type="thinking",
                        )
                    yield StreamChunk(
                        content=result.content,
                        tool_calls=result.tool_calls if result.tool_calls else None,
                        finish_reason=result.finish_reason,
                        usage=result.usage,
                        response_type="tool_call" if result.tool_calls else "content",
                    )
                    return

                lines = resp.aiter_lines().__aiter__()
                async for line in iter_with_stream_idle_timeout(
                    lines,
                    stream_idle_timeout,
                    stage=lambda: stage,
                ):
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        # 某些兼容网关不发 finish_reason；缺省按自然停止处理。
                        if not finish_emitted:
                            final_tool_calls = self._build_tool_calls(tool_call_map)
                            default_finish = "tool_calls" if final_tool_calls else "stop"
                            yield StreamChunk(
                                tool_calls=final_tool_calls if final_tool_calls else None,
                                finish_reason=actual_finish_reason or default_finish,
                                response_type="tool_call" if final_tool_calls else "content",
                            )
                            finish_emitted = True
                        break

                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason") or ""
                    if finish_reason:
                        # [DONE] 只是流边界；真实停止原因必须保留到最后。
                        actual_finish_reason = finish_reason

                    # 防御性检查：某些模型在特定 chunk 中 delta 可能是字符串而非字典
                    if not isinstance(delta, dict):
                        # delta 是字符串时，视为纯 content
                        if delta:
                            yield StreamChunk(
                                content=str(delta),
                                tool_calls=None,
                                finish_reason="",
                                response_type="content",
                            )
                        if finish_reason:
                            final_tool_calls = self._build_tool_calls(tool_call_map)
                            yield StreamChunk(
                                content="",
                                tool_calls=final_tool_calls if final_tool_calls else None,
                                finish_reason=finish_reason,
                                response_type="tool_call" if final_tool_calls else "content",
                            )
                            finish_emitted = True
                        continue

                    # 处理 tool_calls delta（累积模式）
                    delta_tool_calls = delta.get("tool_calls")
                    if delta_tool_calls:
                        stage = "tool_call_generation"
                        for tc_delta in delta_tool_calls:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_call_map:
                                tool_call_map[idx] = {
                                    "id": "",
                                    "function_name": "",
                                    "arguments": "",
                                }
                            entry = tool_call_map[idx]

                            # 累积 id
                            if tc_delta.get("id"):
                                entry["id"] = tc_delta["id"]

                            # 累积 function name
                            func_data = tc_delta.get("function", {})
                            if func_data.get("name"):
                                # 防御性处理：部分 vLLM 实现每个 chunk 重复发送完整名称
                                if entry["function_name"] != func_data["name"]:
                                    entry["function_name"] += func_data["name"]

                            # 累积 arguments
                            if func_data.get("arguments"):
                                entry["arguments"] += func_data["arguments"]

                    # 处理普通 content delta
                    content = delta.get("content") or ""
                    if content:
                        stage = "content"
                        yield StreamChunk(
                            content=content,
                            tool_calls=None,
                            finish_reason="",
                            response_type="content",
                        )

                    # 处理思考内容 delta：
                    # - reasoning_content：DeepSeek/doubao 及 vLLM 旧版 reasoning parser 的字段名
                    # - reasoning：vLLM 0.21+ / 部分 Qwen3 部署使用的字段名
                    # 两者择一即可，做双字段兼容，避免换 vLLM 版本/模型时思考通道丢数据。
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if reasoning:
                        stage = "reasoning"
                        yield StreamChunk(
                            reasoning=reasoning,
                            tool_calls=None,
                            finish_reason="",
                            response_type="thinking",
                        )

                    raw_usage = chunk.get("usage")
                    if raw_usage:
                        stage = "usage"
                        yield StreamChunk(
                            usage=TokenUsage(
                                prompt_tokens=raw_usage.get("prompt_tokens", 0),
                                completion_tokens=raw_usage.get("completion_tokens", 0),
                                total_tokens=raw_usage.get("total_tokens", 0),
                            ),
                            response_type="usage",
                        )

                    # 处理 finish_reason（流结束信号）
                    if finish_reason:
                        stage = "finishing"
                        final_tool_calls = self._build_tool_calls(tool_call_map)
                        yield StreamChunk(
                            content="",
                            tool_calls=final_tool_calls if final_tool_calls else None,
                            finish_reason=finish_reason,
                            response_type="tool_call" if final_tool_calls else "content",
                        )
                        finish_emitted = True

        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"vLLM stream_with_tools 请求失败: HTTP {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise RuntimeError(f"vLLM 连接失败: {e}") from e

    def _build_tool_calls(self, tool_call_map: dict[int, dict]) -> list[LLMToolCall]:
        """从累积的 tool_call_map 构建有序的 LLMToolCall 列表

        Args:
            tool_call_map: 按 index 存储的 tool_call 累积数据

        Returns:
            按 index 排序的 LLMToolCall 列表，空则返回空列表
        """
        if not tool_call_map:
            return []
        result = []
        for idx in sorted(tool_call_map.keys()):
            entry = tool_call_map[idx]
            if entry["function_name"]:  # 只包含有效的 tool_call
                result.append(
                    LLMToolCall(
                        id=entry["id"],
                        function_name=entry["function_name"],
                        arguments=entry["arguments"],
                    )
                )
        return result

    async def close(self):
        """关闭 HTTP 客户端连接"""
        await self._client.aclose()
