"""分组截断压缩 - 从最旧消息组开始移除以控制上下文 token 数

作为三层递进式上下文管理的最后一层兜底策略：当上下文 token 数超过 80% 阈值时，
从最旧的消息组开始移除，直到 token 数降到阈值以下。

压缩时始终保留：
1. system prompt（第一条消息）
2. 当前轮（最后一条 user 消息及其后续所有 assistant/tool 消息，即 tail）
3. tool_call / tool_result 消息组（assistant 含 tool_calls + 后续 tool 消息
   视为一组，压缩时不拆分）

token 计数依赖传入的 TokenEstimator 实例（estimate_message）。
"""

from __future__ import annotations

import logging

from app.agent.memory.context_manager import prune_tool_output

logger = logging.getLogger(__name__)

# 触发压缩的上下文窗口占用比例：超过 max_tokens * 0.8 时从最旧消息组开始截断。
DEFAULT_CONTEXT_THRESHOLD_RATIO = 0.8


def prune_context(
    messages: list[dict],
    max_tokens: int,
    current_tokens: int,
    *,
    tail_start_index: int | None = None,
    threshold_ratio: float = 0.5,
    head_chars: int = 4096,
    tail_chars: int = 1024,
) -> tuple[list[dict], int]:
    """Prune old tool-result text before invoking LLM history compaction."""
    if max_tokens <= 0 or len(messages) <= 2:
        return messages, 0
    threshold = int(max_tokens * threshold_ratio)
    if current_tokens <= threshold:
        return messages, 0

    last_user_idx = tail_start_index
    if last_user_idx is None:
        last_user_idx = len(messages) - 1
        for index in range(len(messages) - 1, 0, -1):
            if messages[index].get("role") == "user":
                last_user_idx = index
                break

    result = list(messages)
    pruned = 0
    for index in range(1, last_user_idx):
        message = result[index]
        if message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        bounded = prune_tool_output(content, head_chars, tail_chars)
        if bounded == content:
            continue
        replacement = dict(message)
        replacement["content"] = bounded
        result[index] = replacement
        pruned += 1

    if pruned:
        logger.debug(
            "[prune_context] pruned %d historical tool results at %d/%d tokens",
            pruned,
            current_tokens,
            max_tokens,
        )
    return result, pruned


def compress_context(
    messages: list[dict],
    estimator,
    max_tokens: int,
    current_tokens: int,
) -> list[dict]:
    """分组截断压缩消息列表，使总 token 数降到阈值以下

    当 current_tokens 超过 max_tokens * DEFAULT_CONTEXT_THRESHOLD_RATIO(0.8)
    时，从最旧的消息组开始移除，累计释放的 token 直到 freed >= tokens_to_free，
    然后重组为 [system_msg] + 剩余组 + tail。

    压缩规则：
    - 始终保留 system prompt（messages[0]）。
    - 始终保留 tail：从最后一条 user 消息到列表末尾（当前轮 user 查询及其后续
      所有 assistant/tool 消息）。
    - history（system 与 tail 之间的中间部分）按 tool_call/tool_result 配对
      分组，从最旧组开始移除，不拆分任何组。

    Args:
        messages: 消息字典列表，messages[0] 约定为 system prompt。
        estimator: TokenEstimator 实例，需提供 estimate_message(msg) -> int。
        max_tokens: 最大上下文窗口 token 数。
        current_tokens: 调用方对当前上下文 token 数的最佳估算。

    Returns:
        压缩后的消息列表。以下情况原样返回不做修改：
        - max_tokens <= 0；
        - 消息列表仅有 2 条或更少；
        - current_tokens 未超过阈值；
        - history 为空（无可压缩的中间消息）。
    """
    # 边界：无效窗口或消息过少（仅 system + 1 条）时不压缩。
    if max_tokens <= 0 or len(messages) <= 2:
        return messages

    threshold = int(max_tokens * DEFAULT_CONTEXT_THRESHOLD_RATIO)

    # 未超过阈值，幂等返回，不做任何修改。
    if current_tokens <= threshold:
        return messages

    system_msg = messages[0]

    # 定位当前轮 user 查询：从尾部向前找最后一条 role == "user" 的消息。
    last_user_idx = len(messages) - 1
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    history = messages[1:last_user_idx]
    tail = messages[last_user_idx:]

    # history 为空说明 system 之后即为当前轮，无可压缩内容。
    if not history:
        return messages

    groups = _group_tool_messages(history)

    tokens_to_free = current_tokens - threshold
    freed = 0
    remove_up_to = 0

    # 从最旧组开始累计释放 token，直到 freed >= tokens_to_free。
    for i, group in enumerate(groups):
        group_tokens = sum(estimator.estimate_message(msg) for msg in group)
        freed += group_tokens
        remove_up_to = i + 1
        if freed >= tokens_to_free:
            break

    # 重组：system + 未被移除的组（展平）+ tail。
    remaining: list[dict] = [system_msg]
    for group in groups[remove_up_to:]:
        remaining.extend(group)
    remaining.extend(tail)

    logger.debug(
        "[compress_context] 压缩完成：%d 条消息 -> %d 条，移除 %d/%d 组，"
        "释放约 %d tokens（目标 %d，当前 %d，阈值 %d）",
        len(messages),
        len(remaining),
        remove_up_to,
        len(groups),
        freed,
        tokens_to_free,
        current_tokens,
        threshold,
    )

    return remaining


def _group_tool_messages(messages: list[dict]) -> list[list[dict]]:
    """将中间历史消息按逻辑单元分组

    分组规则：
    - assistant 消息（含 tool_calls）与其后续连续的 tool 消息视为一组，
      保证 tool_call / tool_result 配对在压缩时不被拆分。
    - 其余独立消息（user、不含 tool_calls 的 assistant 等）各自成组。

    Args:
        messages: 待分组的消息字典列表（history 部分）。

    Returns:
        分组后的消息列表，每个元素为一组消息（list[dict]）。
    """
    groups: list[list[dict]] = []
    i = 0
    n = len(messages)

    while i < n:
        msg = messages[i]

        # assistant + tool_calls：与后续连续的 tool 消息合并为一组。
        if msg.get("role") == "assistant" and (msg.get("tool_calls") or []):
            group = [msg]
            i += 1
            while i < n and messages[i].get("role") == "tool":
                group.append(messages[i])
                i += 1
            groups.append(group)
        else:
            groups.append([msg])
            i += 1

    return groups
