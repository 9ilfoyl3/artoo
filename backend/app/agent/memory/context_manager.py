"""上下文窗口管理 - token 估算、消息压缩、工具输出截断

提供 ContextManager 类和辅助函数，用于管理 Agent 的上下文窗口大小，
避免超出 LLM 的 token 限制。
"""

from __future__ import annotations

import re

PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量

    规则：
    - 中文字符（ord > 0x4E00）按 1.5 字符/token 计算，即每字符贡献 ~0.67 tokens
    - ASCII 字符按 4 字符/token 计算，即每字符贡献 0.25 tokens

    Returns:
        估算的 token 数量
    """
    if not text:
        return 0

    chinese_chars = 0
    ascii_chars = 0

    for ch in text:
        if ord(ch) > 0x4E00:
            chinese_chars += 1
        elif ord(ch) < 128:
            ascii_chars += 1
        else:
            # 其他 Unicode 字符按中文处理
            chinese_chars += 1

    # 中文: 1 token ≈ 1.5 字符 → tokens = chars / 1.5
    # ASCII: 1 token ≈ 4 字符 → tokens = chars / 4
    tokens = chinese_chars / 1.5 + ascii_chars / 4
    return max(1, int(tokens))


def truncate_tool_output(output: str, max_chars: int) -> str:
    """截断工具输出，保留头尾各 40%

    如果 output 长度 <= max_chars，原样返回。
    否则保留原文头部 40% + 截断提示 + 原文尾部 40%。

    Args:
        output: 工具输出文本
        max_chars: 最大字符数

    Returns:
        截断后的文本
    """
    if len(output) <= max_chars:
        return output

    head_size = int(len(output) * 0.4)
    tail_size = int(len(output) * 0.4)
    truncated_count = len(output) - head_size - tail_size

    head = output[:head_size]
    tail = output[-tail_size:]

    return f"{head}[...truncated {truncated_count} chars...]{tail}"


def prune_tool_output(output: str, head_chars: int, tail_chars: int) -> str:
    """Keep bounded head and tail spans from one historical tool result."""
    if not output:
        return output
    if head_chars < 0 or tail_chars < 0:
        return output
    if len(output) <= head_chars + len(PRUNE_MARKER) + tail_chars:
        return output
    head = output[:head_chars] if head_chars else ""
    tail = output[-tail_chars:] if tail_chars else ""
    return f"{head}{PRUNE_MARKER}{tail}"


def redact_historical_kb_results(
    messages: list[dict], current_iteration: int
) -> list[dict]:
    """将非当前轮次的 knowledge_search/grep_chunks 工具结果替换为占位符

    遍历消息列表，找到 role="tool" 的消息，检查其前面的 assistant 消息中
    对应的 tool_call 是否为 knowledge_search 或 grep_chunks。
    如果该工具调用属于之前的迭代（非 current_iteration），则替换内容。

    通过追踪 assistant 消息中的 tool_calls 来确定迭代归属：
    每个 assistant 消息（含 tool_calls）代表一个迭代。

    Args:
        messages: 完整消息列表
        current_iteration: 当前迭代编号（从 0 开始）

    Returns:
        处理后的消息列表
    """
    kb_tools = {"knowledge_search", "grep_chunks"}

    # 追踪每个 tool_call_id 属于哪个迭代
    tool_call_iterations: dict[str, int] = {}
    tool_call_names: dict[str, str] = {}
    iteration = 0

    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                func_info = tc.get("function", {})
                tc_name = func_info.get("name", "")
                if tc_id:
                    tool_call_iterations[tc_id] = iteration
                    tool_call_names[tc_id] = tc_name
            iteration += 1

    # 替换非当前迭代的 KB 工具结果
    result = []
    for msg in messages:
        if msg.get("role") == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_name = tool_call_names.get(tool_call_id, "")
            msg_iteration = tool_call_iterations.get(tool_call_id, -1)

            if tool_name in kb_tools and msg_iteration != current_iteration:
                redacted_msg = dict(msg)
                redacted_msg["content"] = (
                    "[Previous search results redacted - search again if needed]"
                )
                result.append(redacted_msg)
                continue

        result.append(msg)

    return result


class ContextManager:
    """上下文窗口管理器

    负责在 token 超限时压缩消息列表，保留关键信息。
    """

    def compress_messages(
        self, messages: list[dict], max_tokens: int
    ) -> list[dict]:
        """压缩消息列表以适应 token 限制

        策略：
        1. 计算总 token 数
        2. 如果低于阈值（max_tokens * 0.8），原样返回
        3. 否则：保留 system prompt（第一条）+ 最近 2 轮完整消息 + 当前轮 tool results
        4. 中间轮次的 tool results 替换为 "[Summary: {tool_name} returned N results]"

        一轮 = assistant 消息（含 tool_calls）+ 后续的 tool result 消息

        Args:
            messages: 完整消息列表
            max_tokens: 最大 token 数

        Returns:
            压缩后的消息列表
        """
        # 计算总 token
        total_tokens = sum(
            estimate_tokens(msg.get("content", "") or "")
            for msg in messages
        )

        threshold = int(max_tokens * 0.8)
        if total_tokens <= threshold:
            return messages

        if not messages:
            return messages

        # 保留 system prompt（第一条消息）
        result: list[dict] = []
        system_msg = messages[0] if messages[0].get("role") == "system" else None
        if system_msg:
            result.append(system_msg)
            remaining = messages[1:]
        else:
            remaining = messages

        # 识别轮次：一轮 = assistant(tool_calls) + 后续 tool results
        # 用 user 消息作为轮次分隔
        rounds: list[list[int]] = []
        current_round_indices: list[int] = []

        for i, msg in enumerate(remaining):
            role = msg.get("role", "")
            if role == "user" and current_round_indices:
                rounds.append(current_round_indices)
                current_round_indices = [i]
            else:
                current_round_indices.append(i)

        if current_round_indices:
            rounds.append(current_round_indices)

        if len(rounds) <= 2:
            # 只有 2 轮或更少，无法压缩
            return messages

        # 保留最后 2 轮完整消息（4 条 user/assistant + 当前轮 tool results）
        keep_rounds = rounds[-2:]
        keep_start = keep_rounds[0][0]

        # 构建 tool_call_id → tool_name 映射
        tool_call_map: dict[str, str] = {}
        for msg in remaining:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    func_info = tc.get("function", {})
                    tc_name = func_info.get("name", "")
                    if tc_id and tc_name:
                        tool_call_map[tc_id] = tc_name

        # 中间轮次：压缩 tool results
        middle_rounds = rounds[:-2]
        for round_indices in middle_rounds:
            for idx in round_indices:
                msg = remaining[idx]
                role = msg.get("role", "")
                if role == "tool":
                    tool_call_id = msg.get("tool_call_id", "")
                    content = msg.get("content", "")
                    tool_name = tool_call_map.get(tool_call_id, "unknown_tool")
                    result_count = self._count_results(content)
                    summary_msg = dict(msg)
                    summary_msg["content"] = (
                        f"[Summary: {tool_name} returned {result_count} results]"
                    )
                    result.append(summary_msg)
                else:
                    result.append(msg)

        # 追加最后 2 轮完整消息
        for idx in range(keep_start, len(remaining)):
            result.append(remaining[idx])

        return result

    def _count_results(self, content: str) -> int:
        """从工具输出中估算结果数量"""
        if not content:
            return 0
        # 尝试从 XML count 属性提取
        match = re.search(r'count="(\d+)"', content)
        if match:
            return int(match.group(1))
        # 计算 <chunk> 标签数量
        chunk_count = content.count("<chunk ")
        if chunk_count > 0:
            return chunk_count
        # 默认按行数估算
        return max(1, content.count("\n") // 3)
