"""用户自定义技能（Agent Skills）管理接口。

每个用户维护自己的技能（per-user，对齐 ChatSession / AgentPreset 的归属模式）：
- tenant_id：所属租户（方案 B loader criteria 兜底过滤）。
- owner_user_id：创建者（acting_subject_id）。技能是个人资产，仅本人可见可改可用。

预置技能（preloaded/*/SKILL.md）走文件、全局只读，不入此表，也不在本 API 暴露增删改；
对话时由 SkillManager 把「文件预置技能 + 当前用户自定义技能」合并后提供给 Agent。
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import require_member
from app.auth.identity import IdentityContext
from app.schema.db import CustomSkill
from app.storage.database import async_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["Skills"])

# 技能名称约束：read_skill 工具按 name 加载，故需简洁且无歧义。
_NAME_MAX = 100
_DESC_MAX = 500
_INSTRUCTIONS_MAX = 20000

# 保留词：避免技能名与内部工具 / 平台预置技能 / 系统概念冲突（对齐 WeKnora 约束）。
_RESERVED_NAMES = {
    "system", "default", "internal", "core", "base", "root", "admin",
    "final_answer", "thinking", "read_skill", "read_attachment",
    "search_knowledge", "list_documents", "read_document", "todo_write",
    "web_search",
}

# 名称字符集：汉字 / 英文字母 / 数字 / 空格 / 连字符 / 下划线（与 WeKnora 一致，禁特殊符号）。
_NAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9 _-]+$")


class CustomSkillCreate(BaseModel):
    """创建自定义技能请求。"""
    name: str
    description: str
    instructions: str
    enabled: bool = True


class CustomSkillUpdate(BaseModel):
    """更新自定义技能请求（部分更新）。"""
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    enabled: bool | None = None


class SkillGenerateRequest(BaseModel):
    """AI 生成技能请求：用户用一句话描述想要的技能。"""
    instruction: str


class CustomSkillResponse(BaseModel):
    """自定义技能响应。"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    description: str
    instructions: str
    enabled: bool
    created_at: str
    updated_at: str


def _to_response(skill: CustomSkill) -> CustomSkillResponse:
    return CustomSkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        instructions=skill.instructions,
        enabled=skill.enabled,
        created_at=skill.created_at.isoformat() if skill.created_at else "",
        updated_at=skill.updated_at.isoformat() if skill.updated_at else "",
    )


def _validate(name: str, description: str, instructions: str) -> None:
    """字段校验，不通过抛 422。"""
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="技能名称不能为空")
    name = name.strip()
    if len(name) > _NAME_MAX:
        raise HTTPException(status_code=422, detail=f"技能名称过长，最大 {_NAME_MAX} 字符")
    if not _NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=422,
            detail="技能名称只能包含汉字、英文字母、数字、空格、连字符或下划线",
        )
    if name.lower() in _RESERVED_NAMES:
        raise HTTPException(status_code=422, detail=f"「{name}」是保留名称，请换一个")
    if not description or not description.strip():
        raise HTTPException(status_code=422, detail="技能描述不能为空")
    if len(description) > _DESC_MAX:
        raise HTTPException(status_code=422, detail=f"技能描述过长，最大 {_DESC_MAX} 字符")
    if not instructions or not instructions.strip():
        raise HTTPException(status_code=422, detail="技能指令内容不能为空")
    if len(instructions) > _INSTRUCTIONS_MAX:
        raise HTTPException(status_code=422, detail=f"指令内容过长，最大 {_INSTRUCTIONS_MAX} 字符")


async def _get_owned_or_404(
    session, skill_id: str, identity: IdentityContext
) -> CustomSkill:
    """按 id 取技能并校验归属本人，否则 404（存在性非泄露）。"""
    result = await session.execute(select(CustomSkill).where(CustomSkill.id == skill_id))
    skill = result.scalar_one_or_none()
    subject = identity.acting_subject_id
    if skill is None or subject is None or skill.owner_user_id != subject:
        raise HTTPException(status_code=404, detail="技能不存在")
    return skill


async def load_user_custom_skills(owner_user_id: str | None):
    """加载某用户启用的自定义技能，返回 Skill 列表（供 SkillManager extra_skills 注入）。

    owner_user_id 为 None（机器身份等无自然人主体）时返回空列表——技能是个人资产。
    在 chat 链路构建 Agent 时调用，与文件预置技能合并。
    """
    from app.agent.skills.loader import Skill

    if not owner_user_id:
        return []

    async with async_session() as session:
        result = await session.execute(
            select(CustomSkill).where(
                CustomSkill.owner_user_id == owner_user_id,
                CustomSkill.enabled == True,  # noqa: E712
            )
        )
        rows = result.scalars().all()

    return [
        Skill(
            name=r.name,
            description=r.description,
            instructions=r.instructions,
            base_path="",  # DB 技能无文件目录
        )
        for r in rows
    ]


@router.post("/generate")
async def generate_skill(
    data: SkillGenerateRequest,
    _identity: IdentityContext = Depends(require_member()),
):
    """用默认模型把用户的一句话描述生成结构化技能（name + description + instructions）。

    复用 agent_config rewrite-prompt 的范式。产出的 instructions 是技能被加载后 Agent
    将遵循的完整操作指南，只描述「该技能擅长什么、何时用、按什么步骤做、输出什么」，
        不复述检索流程 / 工具调用 / 终止条件等底层规则（那些由核心提示词负责）。

    返回 {name, description, instructions} 供前端回填表单，用户确认后再保存。
    """
    from app.api.chat import _get_llm_for_request

    instruction = (data.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="请描述你想要的技能")

    llm, _, _ = await _get_llm_for_request(None)

    meta_system = (
        "你是一名 Agent 技能设计助手，为「知识库问答助手」设计一个可复用的【技能】。"
        "技能采用渐进式披露：平时只把 name+description 展示给智能体，当用户问题匹配该"
        "描述时，智能体才加载 instructions 全文并遵循执行。\n\n"
        "请根据用户的描述，输出一个 JSON 对象，且只输出 JSON（不要任何前言、解释或代码块包裹）：\n"
        "{\n"
        '  "name": "技能名称（汉字/英文/数字/空格/连字符/下划线，简洁，≤30字）",\n'
        '  "description": "一句话说明技能擅长什么、何时触发（如「当用户需要……时使用」），≤200字",\n'
        '  "instructions": "技能被加载后智能体遵循的完整操作指南，Markdown 格式"\n'
        "}\n\n"
        "instructions 写作要求：\n"
        "1. 聚焦该技能特有的方法论：适用场景、工作步骤、输出格式、注意事项。\n"
        "2. 智能体唯一的召回工具是 search_knowledge，支持 hybrid / semantic / "
        "keyword / summary 模式；文档读取使用 read_document。"
        "可以引用这些工具来编排步骤，但不要复述它们的底层调用规则、引用格式、语言要求"
        "——这些已由核心提示词负责。\n"
        "3. 不要编造智能体没有的能力（如执行脚本、联网下载、调用外部 API）。\n"
        "4. 用与用户描述一致的语言书写（用户用中文则用中文）。\n"
        "5. name 不得为以下保留词：" + "、".join(sorted(_RESERVED_NAMES)) + "。"
    )
    meta_user = f"【用户描述】\n{instruction}\n\n请据此设计技能并输出 JSON。"

    try:
        raw = await llm.generate(
            [
                {"role": "system", "content": meta_system},
                {"role": "user", "content": meta_user},
            ],
            temperature=0.7,
        )
    except Exception as e:
        logger.exception("AI 生成技能失败")
        raise HTTPException(status_code=502, detail=f"AI 生成失败：{e}")

    parsed = _parse_skill_json(raw or "")
    if parsed is None:
        raise HTTPException(status_code=502, detail="AI 返回格式异常，请重试或换种描述")

    # 软校验：超长截断而非报错（生成结果供用户编辑确认，不强制拦截）
    name = parsed.get("name", "").strip()[:_NAME_MAX]
    description = parsed.get("description", "").strip()[:_DESC_MAX]
    instructions = parsed.get("instructions", "").strip()[:_INSTRUCTIONS_MAX]
    if not (name and description and instructions):
        raise HTTPException(status_code=502, detail="AI 返回内容不完整，请重试")

    return {"name": name, "description": description, "instructions": instructions}


def _parse_skill_json(raw: str) -> dict | None:
    """从模型输出中提取技能 JSON。容错处理 ``` 代码块包裹与首尾杂字。"""
    text = raw.strip()
    # 去掉 ``` / ```json 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # 直接解析；失败则截取第一个 { 到最后一个 } 再试
    for candidate in (text, _slice_braces(text)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _slice_braces(text: str) -> str:
    """截取首个 '{' 到末个 '}' 的子串（兜底解析）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return ""


@router.get("", response_model=list[CustomSkillResponse])
async def list_skills(identity: IdentityContext = Depends(require_member())):
    """列出当前用户自己的自定义技能（按创建时间倒序）。"""
    subject = identity.acting_subject_id
    if subject is None:
        return []
    async with async_session() as session:
        result = await session.execute(
            select(CustomSkill)
            .where(CustomSkill.owner_user_id == subject)
            .order_by(CustomSkill.created_at.desc())
        )
        skills = result.scalars().all()
    return [_to_response(s) for s in skills]


@router.post("", response_model=CustomSkillResponse, status_code=201)
async def create_skill(
    data: CustomSkillCreate,
    identity: IdentityContext = Depends(require_member()),
):
    """创建自定义技能。归属盖章：tenant_id=当前租户、owner_user_id=行事主体。"""
    _validate(data.name, data.description, data.instructions)
    subject = identity.acting_subject_id
    if subject is None:
        raise HTTPException(status_code=403, detail="当前身份无法创建个人技能")

    async with async_session() as session:
        # 同一用户下技能名唯一（read_skill 按 name 加载，重名会产生歧义）
        dup = await session.execute(
            select(CustomSkill).where(
                CustomSkill.owner_user_id == subject,
                CustomSkill.name == data.name.strip(),
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="已存在同名技能")

        skill = CustomSkill(
            id=str(uuid.uuid4()),
            owner_user_id=subject,
            tenant_id=identity.tenant_id,
            name=data.name.strip(),
            description=data.description.strip(),
            instructions=data.instructions,
            enabled=data.enabled,
        )
        session.add(skill)
        await session.commit()
        await session.refresh(skill)
        return _to_response(skill)


@router.put("/{skill_id}", response_model=CustomSkillResponse)
async def update_skill(
    skill_id: str,
    data: CustomSkillUpdate,
    identity: IdentityContext = Depends(require_member()),
):
    """更新自定义技能（仅创建者本人）。"""
    async with async_session() as session:
        skill = await _get_owned_or_404(session, skill_id, identity)

        new_name = data.name.strip() if data.name is not None else skill.name
        new_desc = data.description.strip() if data.description is not None else skill.description
        new_inst = data.instructions if data.instructions is not None else skill.instructions
        _validate(new_name, new_desc, new_inst)

        # 改名时查重（排除自身）
        if data.name is not None and new_name != skill.name:
            dup = await session.execute(
                select(CustomSkill).where(
                    CustomSkill.owner_user_id == skill.owner_user_id,
                    CustomSkill.name == new_name,
                    CustomSkill.id != skill_id,
                )
            )
            if dup.scalar_one_or_none() is not None:
                raise HTTPException(status_code=409, detail="已存在同名技能")

        skill.name = new_name
        skill.description = new_desc
        skill.instructions = new_inst
        if data.enabled is not None:
            skill.enabled = data.enabled

        await session.commit()
        await session.refresh(skill)
        return _to_response(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: str,
    identity: IdentityContext = Depends(require_member()),
):
    """删除自定义技能（仅创建者本人）。"""
    async with async_session() as session:
        skill = await _get_owned_or_404(session, skill_id, identity)
        await session.delete(skill)
        await session.commit()
