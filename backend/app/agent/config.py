"""AgentConfig - ReAct Agent 引擎配置"""

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """ReAct Agent 运行配置

    控制 Agent 引擎的行为参数，包括迭代限制、工具白名单、
    LLM 参数、上下文窗口管理等。
    """

    # 最大迭代次数，达到上限时强制合成最终答案
    max_iterations: int = 20

    # 允许使用的工具列表（白名单）
    allowed_tools: list[str] = field(default_factory=lambda: [
        "knowledge_search", "grep_chunks", "list_knowledge_chunks", "read_document",
        "list_knowledge_documents", "summarize_documents", "calculate",
    ])

    # LLM 生成温度
    temperature: float = 0.7

    # 关联的知识库 ID 列表
    knowledge_base_ids: list[str] = field(default_factory=list)

    # 是否启用网页搜索工具
    web_search_enabled: bool = False

    # 是否开启模型原生 thinking / reasoning 通道；不是 ThinkingTool。
    thinking_enabled: bool = True

    # 可选的 provider 原生 reasoning effort；仅显式配置时透传，避免不支持该字段的端点拒绝请求。
    reasoning_effort: str | None = None

    # 是否允许并行执行多个工具调用
    parallel_tool_calls: bool = False

    # P0：完全相同工具调用达到阈值时向模型注入 advisory reminder。
    repeat_tool_reminder_enabled: bool = True

    # P0：检索工具内部规范化并去重 query。
    query_dedup_enabled: bool = True

    # P1：低质量检索最多允许一次由模型自行改写 query 的机会。
    query_rewrite_max_attempts: int = 1

    # P2：上下文压力出现时先裁剪旧工具结果，再决定是否触发 LLM 摘要。
    context_pruning_enabled: bool = True
    context_prune_threshold: float = 0.5
    context_prune_head_chars: int = 4096
    context_prune_tail_chars: int = 1024

    # P3：agent 的确定性检索结果缓存；只在非 degraded、非空结果时写入。
    retrieval_cache_enabled: bool = True

    # 最大上下文 token 数，超过阈值触发压缩
    max_context_tokens: int = 200000

    # 单次 LLM 输出上限；None 表示沿用模型服务的默认值。
    # OpenAI 兼容请求里的 max_tokens 透传到这里，避免长答案被默认上限截断后
    # 被误标为自然完成。
    max_output_tokens: int | None = None

    # LLM 单次调用超时时间（秒）
    llm_call_timeout: int = 120

    # 工具输出最大字符数，超过时截断（保留头尾）
    max_tool_output_chars: int = 16000

    # 记忆合并触发阈值（占 max_context_tokens 的比例，有效范围 0-1）
    consolidation_threshold: float = 0.5

    # 是否保留历史检索结果（为 True 时不对历史 KB 工具结果做脱敏处理）
    retain_retrieval_history: bool = False

    # 完整系统提示词（运行时已渲染好的最终 prompt）。非空时引擎直接原样使用，
    # 不再叠加核心模板——chat 链路会把渲染结果回填到这里供引擎使用。
    system_prompt: str = ""

    # P3：动态时钟不再写入稳定 system prompt，而是在当前用户轮末尾注入。
    timezone_name: str | None = None

    # 用户自定义指令（角色设定 / 语气 / 工作流方法论 / 边界约束）。
    # chat 链路会渲染进 system_prompt；仅当 system_prompt 为空且引擎自行渲染
    # 默认模板时，render_system_prompt 才直接消费此字段。
    custom_instructions: str = ""
