"""Agent 预设配置 API

管理 Agent 运行预设（max_iterations、temperature、thinking_enabled 等），
支持 CRUD 操作和内置预设自动创建。
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_member, require_platform
from app.auth.identity import IdentityContext
from app.schema.db import AgentPreset
from app.storage.database import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent-presets", tags=["Agent Config"])


# ============ Pydantic Models ============

class AgentPresetCreate(BaseModel):
    """创建 Agent 预设请求"""
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=500)
    config_json: dict = Field(default_factory=dict)
    # 平台唯一默认；仅超级管理员可置 True，普通成员写入会被拒绝。
    is_default: bool = False
    # 是否开放给本租户全体成员可见可用（默认私有，仅创建者可见）
    is_shared: bool = False


class AgentPresetUpdate(BaseModel):
    """更新 Agent 预设请求"""
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    config_json: dict | None = None
    # 平台唯一默认；仅超级管理员可修改。
    is_default: bool | None = None
    # 开放/收回开关（None 表示不变更）
    is_shared: bool | None = None


class AgentPresetResponse(BaseModel):
    """Agent 预设响应"""
    id: str
    name: str
    description: str | None
    config_json: dict | None
    is_default: bool
    created_at: str
    updated_at: str
    # 归属与可见性（agent-preset-sharing）
    is_shared: bool = False          # 是否开放给本租户全体成员
    is_builtin: bool = False         # 是否平台内置预设（任何人不可改删）
    is_owner: bool = False           # 当前请求者是否为创建者（决定前端是否显示改/删/开放开关）
    owner_user_id: str | None = None
    owner_username: str | None = None  # 创建者用户名（供"来自 xxx"展示；内置预设为 None）


class PromptRewriteRequest(BaseModel):
    """AI 生成自定义指令请求"""
    instruction: str = Field(..., max_length=2000, description="用户描述的角色与特性")
    current_prompt: str | None = Field(default=None, max_length=20000, description="当前已有的自定义指令（可选，作为改写基础）")


class ToolOption(BaseModel):
    """可配进 allowed_tools 的单个工具选项。

    ``name`` 是写入预设 ``allowed_tools`` 的稳定契约值；``label`` 仅供展示。
    MCP 工具的 ``name`` 已是带 ``tool_prefix`` 的展示名（与
    ``chat._register_mcp_tools`` 的比对口径一致，见 MCPServerSpec.display_tool_name）。
    """
    name: str
    label: str
    description: str = ""
    source: str  # builtin | mcp
    # 基础设施工具：不受白名单管控，恒注册（见 chat._build_agent_runtime 的 _INFRA_TOOLS）。
    # 前端据此把开关显示为"始终启用"，避免用户误以为取消勾选就能关掉。
    always_on: bool = False
    # MCP 工具来源 server 名（仅展示与分组用）。**绝不回 url / 凭据**——那是平台底座配置。
    server_name: str | None = None


class AvailableToolsResponse(BaseModel):
    """可用工具清单（内置 + 外部 MCP 发现）。

    ``mcp_error`` 非空表示外部工具发现失败（远端不可达 / DB 异常）。此时内置工具
    照常返回，不让第三方故障阻塞预设编辑。
    """
    tools: list[ToolOption]
    mcp_error: str | None = None


# ============ 内置预设 ============
# 内置预设：平台级、跨租户全员可见可用、任何人不可改删。
# owner_user_id=None + tenant_id=None + is_shared=True 标识其内置身份。

_BUILTIN_PRESET_IDS = {"preset-quick-qa", "preset-smart-reasoning"}


def _is_builtin(preset: AgentPreset) -> bool:
    """是否平台内置预设（任何人不可改删）。"""
    return preset.id in _BUILTIN_PRESET_IDS


_BUILTIN_PRESETS = [
    {
        "id": "preset-quick-qa",
        "name": "快速问答",
        "description": "单轮检索直接作答，适合简单问题，快速返回结果",
        "config_json": {
            "agent_mode": "hybrid",
            "max_iterations": 5,
            "thinking_enabled": False,
            "temperature": 0.3,
        },
        "is_default": False,
        # 内置：跨租户全员可见、无归属、不可改删
        "tenant_id": None,
        "owner_user_id": None,
        "is_shared": True,
    },
    {
        "id": "preset-smart-reasoning",
        "name": "智能推理",
        "description": "ReAct 多步推理，深度思考和多轮检索，适合复杂问题",
        "config_json": {
            "agent_mode": "agent",
            "max_iterations": 20,
            "thinking_enabled": True,
            "temperature": 0.7,
        },
        "is_default": True,
        "tenant_id": None,
        "owner_user_id": None,
        "is_shared": True,
    },
]


# ============ 内置工具清单 ============
# 可配进 allowed_tools 的内置工具。取值与 chat._build_agent_runtime 的注册分支一一对应，
# 是前端工具勾选面板的单一真值来源（此前硬编码在 AgentConfig.tsx，新增工具易漏）。
#
# 回答由自然停止的 assistant text 承载；native reasoning 是模型通道，不是可配置工具。
# read_attachment / read_skill 由"本条消息是否带附件""是否存在技能"动态决定，
# 不属于预设可配项，故不出现在此清单。

_BUILTIN_TOOL_CATALOG: list[dict] = [
    {
        "name": "knowledge_search",
        "label": "语义检索",
        "description": "在所选知识库与会话文件中做向量 + 稀疏混合检索",
    },
    {
        "name": "grep_chunks",
        "label": "关键词检索",
        "description": "在主知识库内按关键词精确匹配分块",
    },
    {
        "name": "list_knowledge_chunks",
        "label": "分页浏览",
        "description": "按原文顺序分页浏览文档分块",
    },
    {
        "name": "web_search",
        "label": "网页搜索",
        "description": "检索公开网页（需平台已配置 SearXNG，未配置时勾选无效）",
    },
]


async def get_effective_preset_config(
    preset_id: str | None, identity: IdentityContext | None = None
) -> dict:
    """获取生效的 Agent 预设配置（config_json）

    指定 preset_id 时返回该预设；否则返回默认预设（is_default=True）。
    都找不到时返回智能推理内置预设的配置作为兜底。

    可见性收敛（agent-preset-sharing）：当传入 identity 且显式指定了 preset_id 时，
    校验该预设在请求者可见范围内（内置 ∪ 自有 ∪ 本租户已开放），不可见则忽略该
    preset_id 回退默认预设——防止凭 id 使用他人私有预设。identity 为 None（如内部
    无身份调用 / 标题生成）时不做可见性校验，保持兼容。

    Returns:
        预设的 config_json 字典，至少包含 agent_mode / max_iterations /
        temperature / thinking_enabled（取自数据库或内置兜底）。
    """
    await _ensure_builtin_presets()

    async with async_session() as session:
        preset = None
        if preset_id:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.id == preset_id)
            )
            preset = result.scalar_one_or_none()
            # 指定了 preset 但不可见 → 视为未指定，回退默认（不泄露/不越权使用他人私有预设）
            if preset is not None and identity is not None and not _is_visible(preset, identity):
                # 显式记录这次静默回退：预设决定 allowed_tools（含外部 MCP 工具白名单），
                # 回退后工具集与调用方预期完全不同（表现为"Agent 不调工具"），
                # 而不记日志时现场只能看到"行为不对"却查不到原因。外部租户尤其易踩：
                # 其 tenant_id 硬锁 External_User_Tenant，看不到业务租户下创建的预设。
                logger.warning(
                    "[AgentPreset] 预设 %s 对当前身份不可见，已回退默认预设："
                    "preset_tenant=%s owner=%s is_shared=%s | caller_tenant=%s subject=%s。"
                    "工具白名单将取自默认预设。",
                    preset_id,
                    preset.tenant_id,
                    preset.owner_user_id,
                    preset.is_shared,
                    identity.tenant_id,
                    identity.acting_subject_id,
                )
                preset = None
            elif preset is None:
                logger.warning(
                    "[AgentPreset] 指定的预设 %s 不存在，已回退默认预设。", preset_id
                )
        if preset is None:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.is_default == True)  # noqa: E712
            )
            preset = result.scalar_one_or_none()

    if preset is not None and preset.config_json:
        return dict(preset.config_json)

    # 兜底：内置智能推理预设
    return dict(_BUILTIN_PRESETS[1]["config_json"])


def _is_visible(preset: AgentPreset, identity: IdentityContext) -> bool:
    """判断某预设对当前身份是否可见可用。

    可见 = 内置预设（tenant_id 为 None）∪ 本租户内自有（owner==我）∪ 本租户内已开放
    （is_shared 且同租户）。管理员无特权——看不到他人未开放的私有预设。
    """
    # 内置预设：跨租户全员可见
    if _is_builtin(preset) or preset.tenant_id is None:
        return True
    # 仅本租户范围内
    if preset.tenant_id != identity.tenant_id:
        return False
    subject = identity.acting_subject_id
    # 自有
    if preset.owner_user_id is not None and subject is not None and preset.owner_user_id == subject:
        return True
    # 本租户已开放
    return bool(preset.is_shared)


async def _promote_global_default(session: AsyncSession, preset: AgentPreset) -> None:
    """将 ``preset`` 设为唯一全局默认预设。"""
    result = await session.execute(
        select(AgentPreset).where(
            AgentPreset.is_default == True,  # noqa: E712
            AgentPreset.id != preset.id,
        )
    )
    for other in result.scalars().all():
        other.is_default = False
    preset.is_default = True


async def _ensure_builtin_presets() -> None:
    """确保内置预设存在，并维持恰好一个全局默认预设。

    已存在则校正内置归属字段。历史数据可能因旧的“每人一个默认”语义存在零个或
    多个默认项，这里统一收敛为一项：优先保留内置“智能推理”，否则保留最早创建的
    默认项；没有任何默认项时回落到内置“智能推理”。
    """
    async with async_session() as session:
        for preset_data in _BUILTIN_PRESETS:
            result = await session.execute(
                select(AgentPreset).where(AgentPreset.id == preset_data["id"])
            )
            existing = result.scalar_one_or_none()
            if not existing:
                preset = AgentPreset(**preset_data)
                session.add(preset)
                logger.info("创建内置 Agent 预设: %s", preset_data["name"])
            else:
                # 校正内置归属（兼容升级前已存在、缺归属字段的旧内置行）
                existing.tenant_id = None
                existing.owner_user_id = None
                existing.is_shared = True

        await session.flush()
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.is_default == True)  # noqa: E712
        )
        defaults = list(result.scalars().all())
        if not defaults:
            fallback = await session.get(AgentPreset, "preset-smart-reasoning")
            if fallback is not None:
                fallback.is_default = True
        elif len(defaults) > 1:
            defaults.sort(
                key=lambda preset: (
                    preset.id != "preset-smart-reasoning",
                    preset.created_at.isoformat() if preset.created_at else "",
                    preset.id,
                )
            )
            for duplicate in defaults[1:]:
                duplicate.is_default = False
        await session.commit()


def _to_response(
    preset: AgentPreset,
    identity: IdentityContext | None = None,
    owner_username: str | None = None,
) -> AgentPresetResponse:
    """ORM 模型转响应。

    identity 提供时计算 is_owner（前端据此显示改/删/开放开关）；内置预设 is_builtin=True。
    owner_username 由调用方批量解析后传入（供"来自 xxx"展示），内置预设无归属为 None。
    """
    builtin = _is_builtin(preset)
    is_owner = False
    if identity is not None and not builtin:
        subject = identity.acting_subject_id
        is_owner = (
            preset.owner_user_id is not None
            and subject is not None
            and preset.owner_user_id == subject
        )
    return AgentPresetResponse(
        id=preset.id,
        name=preset.name,
        description=preset.description,
        config_json=preset.config_json,
        is_default=preset.is_default,
        created_at=preset.created_at.isoformat() if preset.created_at else "",
        updated_at=preset.updated_at.isoformat() if preset.updated_at else "",
        is_shared=bool(preset.is_shared),
        is_builtin=builtin,
        is_owner=is_owner,
        owner_user_id=preset.owner_user_id,
        owner_username=None if builtin else owner_username,
    )


# ============ CRUD Endpoints ============

@router.post("/rewrite-prompt")
async def rewrite_prompt(
    data: PromptRewriteRequest,
    _identity: IdentityContext = Depends(require_member()),
):
    """用默认模型把用户的自然语言描述润色成一段可直接使用的「自定义指令」

    自定义指令只描述角色设定、语气、工作流方法论与边界约束，会被追加在固定的核心
    系统提示词之后，绝不覆盖核心检索纪律。因此这里产出的也只是这一段附加指令，
    不包含、也不需要复述核心 Progressive RAG 结构与 final_answer 等硬性规则。
    """
    from app.api.chat import _get_llm_for_request

    instruction = (data.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="请描述你想要的角色与特性")

    # 使用默认模型（model_config_id=None → 走数据库默认配置 → 全局配置兜底）
    llm, _, _ = await _get_llm_for_request(None)

    meta_system = (
        "你是一名提示词写作助手，负责为「知识库问答助手」编写一段【自定义指令】。"
        "这段自定义指令会被追加到一份固定的核心系统提示词之后，用来定制助手的"
        "角色设定、语气风格、工作方法论和边界约束。\n\n"
        "硬性要求：\n"
        "1. 只输出这段自定义指令本身，不要复述检索流程、工具调用、引用格式、"
        "终止条件等底层规则——这些已由核心提示词负责，你无需也不要涉及。\n"
        "2. 聚焦于：角色定位、语气与表达风格、回答时的侧重与方法、可做与不可做的边界。\n"
        "3. 用与用户描述一致的语言书写（用户用中文则用中文），简洁清晰，可用要点列举。\n"
        "4. 不要使用占位符变量，不要包含花括号模板。\n"
        "5. 不要任何前言、解释或代码块包裹，直接输出指令正文。"
    )

    base = (data.current_prompt or "").strip()
    if base:
        meta_user = (
            "【已有的自定义指令】\n"
            f"{base}\n\n"
            "【用户希望调整或补充的角色与特性】\n"
            f"{instruction}\n\n"
            "请在已有指令基础上融合用户的新要求，产出润色后的完整自定义指令。"
        )
    else:
        meta_user = (
            "【用户希望的角色与特性】\n"
            f"{instruction}\n\n"
            "请据此产出一段完整的自定义指令。"
        )

    try:
        rewritten = await llm.generate(
            [
                {"role": "system", "content": meta_system},
                {"role": "user", "content": meta_user},
            ],
            temperature=0.7,
        )
    except Exception as e:
        logger.exception("AI 生成自定义指令失败")
        raise HTTPException(status_code=502, detail=f"AI 生成失败：{e}")

    rewritten = (rewritten or "").strip()
    if not rewritten:
        raise HTTPException(status_code=502, detail="AI 返回为空，请重试")

    # 去除模型可能包裹的 ``` 代码块
    if rewritten.startswith("```"):
        lines = rewritten.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        rewritten = "\n".join(lines).strip()

    return {"prompt": rewritten}


@router.get("/available-tools", response_model=AvailableToolsResponse)
async def list_available_tools(
    _identity: IdentityContext = Depends(require_member()),
):
    """列出可写入预设 allowed_tools 的工具（内置 + 已启用 MCP server 发现的外部工具）。

    存在的必要性：外部 MCP 工具是 **default-off**，必须显式出现在预设 allowed_tools
    中才会注册（见 chat._register_mcp_tools）。而 MCP server 配置属平台底座、仅超管
    可见（mcp_config.py 挂 require_platform），租户侧此前无从得知远端声明了哪些工具名，
    只能靠直接 PUT config_json 手写——本端点把这份"工具名清单"以最小暴露面开放给成员。

    暴露边界：只回工具名 / 描述 / 来源 server 名，**不回 url、不回凭据、不回 transport
    等任何平台侧连接配置**，故不构成对 require_platform 的绕过。

    发现失败（远端不可达等）不报错：内置工具照常返回，mcp_error 回带原因，避免第三方
    故障把预设编辑页整个卡死。工具发现有 300s 模块级缓存，不会每次打开面板都打远端。
    """
    tools = [
        ToolOption(
            name=item["name"],
            label=item["label"],
            description=item.get("description", ""),
            source="builtin",
            always_on=bool(item.get("always_on", False)),
        )
        for item in _BUILTIN_TOOL_CATALOG
    ]

    mcp_error: str | None = None
    try:
        from app.agent.tools.mcp_client import get_mcp_tools

        for mcp_tool in await get_mcp_tools():
            tools.append(
                ToolOption(
                    name=mcp_tool.name,
                    label=mcp_tool.name,  # 外部工具名即契约，不另造中文名以免与远端失同步
                    description=mcp_tool.description or "",
                    source="mcp",
                    server_name=mcp_tool.server_name or None,
                )
            )
    except Exception as e:
        logger.warning("外部 MCP 工具发现失败，仅返回内置工具: %s", e)
        mcp_error = str(e)

    return AvailableToolsResponse(tools=tools, mcp_error=mcp_error)


@router.get("", response_model=list[AgentPresetResponse])
async def list_presets(
    identity: IdentityContext = Depends(require_member()),
):
    """获取当前身份可见的 Agent 预设列表（成员及以上可访问）。

    可见范围（agent-preset-sharing）：内置预设 ∪ 本租户内自有 ∪ 本租户内已开放。
    管理员无特权——看不到他人未开放的私有预设。
    """
    # 确保内置预设存在
    await _ensure_builtin_presets()

    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).order_by(AgentPreset.created_at)
        )
        all_presets = result.scalars().all()

        visible = [p for p in all_presets if _is_visible(p, identity)]

        # 批量解析创建者用户名（供前端"来自 xxx"展示；内置预设无归属跳过）
        owner_ids = {
            p.owner_user_id for p in visible
            if p.owner_user_id is not None and not _is_builtin(p)
        }
        username_map: dict[str, str] = {}
        if owner_ids:
            from app.schema.db import User
            rows = await session.execute(
                select(User.id, User.username).where(User.id.in_(list(owner_ids)))
            )
            username_map = {uid: uname for uid, uname in rows.all()}

    return [
        _to_response(p, identity, username_map.get(p.owner_user_id or ""))
        for p in visible
    ]


@router.put("/{preset_id}/default", response_model=AgentPresetResponse)
async def set_default_preset(
    preset_id: str,
    identity: IdentityContext = Depends(require_platform()),
):
    """将平台内置或超管创建的平台级预设设为全局默认（仅超级管理员）。"""
    await _ensure_builtin_presets()

    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.id == preset_id)
        )
        preset = result.scalar_one_or_none()
        if preset is None or not _is_visible(preset, identity):
            raise HTTPException(status_code=404, detail="预设不存在")

        await _promote_global_default(session, preset)
        await session.commit()
        await session.refresh(preset)

    return _to_response(preset, identity)


@router.post("", response_model=AgentPresetResponse)
async def create_preset(
    data: AgentPresetCreate,
    identity: IdentityContext = Depends(require_member()),
):
    """创建新的 Agent 预设（成员及以上）。

    归属盖章：tenant_id=当前租户、owner_user_id=行事主体。可选 is_shared 开放给本租户。
    """
    if data.is_default and not identity.is_super_admin:
        raise HTTPException(status_code=403, detail="仅平台超级管理员可以设置全局默认智能体")

    preset = AgentPreset(
        id=str(uuid.uuid4()),
        name=data.name,
        description=data.description,
        config_json=data.config_json,
        is_default=data.is_default,
        tenant_id=identity.tenant_id,
        owner_user_id=identity.acting_subject_id,
        is_shared=bool(data.is_shared),
    )

    async with async_session() as session:
        if data.is_default:
            await _promote_global_default(session, preset)

        session.add(preset)
        await session.commit()
        await session.refresh(preset)

    return _to_response(preset, identity)


def _ensure_owner_or_404(preset: AgentPreset | None, identity: IdentityContext) -> AgentPreset:
    """管理权校验：仅创建者本人可改/删；内置预设禁改删。

    - 不存在 / 不可见 → 404（不泄露存在性）。
    - 内置预设 → 403（任何人不可改删）。
    - 可见但非创建者（含管理员、被开放的使用者）→ 403。
    """
    if preset is None or not _is_visible(preset, identity):
        raise HTTPException(status_code=404, detail="预设不存在")
    if _is_builtin(preset):
        raise HTTPException(status_code=403, detail="内置预设不可修改或删除")
    subject = identity.acting_subject_id
    if not (preset.owner_user_id is not None and subject is not None and preset.owner_user_id == subject):
        raise HTTPException(status_code=403, detail="只有创建者本人可以修改或删除该预设")
    return preset


@router.put("/{preset_id}", response_model=AgentPresetResponse)
async def update_preset(
    preset_id: str,
    data: AgentPresetUpdate,
    identity: IdentityContext = Depends(require_member()),
):
    """更新 Agent 预设（仅创建者本人；内置预设不可改）。"""
    if data.is_default is not None and not identity.is_super_admin:
        raise HTTPException(status_code=403, detail="仅平台超级管理员可以设置全局默认智能体")
    if data.is_default is False:
        await _ensure_builtin_presets()

    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.id == preset_id)
        )
        preset = _ensure_owner_or_404(result.scalar_one_or_none(), identity)
        was_default = preset.is_default

        if data.name is not None:
            preset.name = data.name
        if data.description is not None:
            preset.description = data.description
        if data.config_json is not None:
            preset.config_json = data.config_json
        if data.is_shared is not None:
            preset.is_shared = data.is_shared
        if data.is_default is not None:
            if data.is_default:
                await _promote_global_default(session, preset)
            else:
                preset.is_default = False
                if was_default:
                    fallback = await session.get(AgentPreset, "preset-smart-reasoning")
                    if fallback is not None:
                        await _promote_global_default(session, fallback)

        await session.commit()
        await session.refresh(preset)

    return _to_response(preset, identity)


@router.delete("/{preset_id}")
async def delete_preset(
    preset_id: str,
    identity: IdentityContext = Depends(require_member()),
):
    """删除 Agent 预设（仅创建者本人；内置预设不可删）。"""
    await _ensure_builtin_presets()

    async with async_session() as session:
        result = await session.execute(
            select(AgentPreset).where(AgentPreset.id == preset_id)
        )
        preset = _ensure_owner_or_404(result.scalar_one_or_none(), identity)

        if preset.is_default:
            fallback = await session.get(AgentPreset, "preset-smart-reasoning")
            if fallback is not None:
                await _promote_global_default(session, fallback)

        await session.delete(preset)
        await session.commit()

    return {"detail": "已删除"}
