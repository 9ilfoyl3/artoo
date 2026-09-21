"""MCP Server 配置管理接口

MCP server 属平台底座（capability-config-to-platform），全平台一份，无 tenant_id，
仅超级管理员维护（require_platform 守卫）。Agent 运行时按此发现远端 MCP server 的
工具注入（default-off，须预设 allowed_tools 显式白名单才注册，见 chat._register_mcp_tools）。

本轮（mcp-standard-protocol）新增三组能力，全部按"默认不改变既有行为"设计：

- ``transport``：``auto``（默认，先试标准 MCP 再回落私有 REST）/ ``streamable_http`` /
  ``legacy_rest``。
- 静态凭据 ``auth_type`` + ``auth_token``：token 经 :mod:`app.auth.secret_box` 加密落库，
  **响应只回掩码**，前端不回显、不可读回明文。
- ``forward_context``：是否向该 server 透传调用方上下文（会话 / 租户 / 主体），
  默认关闭（跨方隐私边界）。

保存前对 URL 做 SSRF 校验（含域名解析检查），拒绝 link-local 等危险目标。

配置变更经 capability_reload 广播：本进程立即失效工具发现 / 传输探测 / 握手缓存，
多进程部署时其他 API 进程经 InvalidationBus 同步失效（Redis 不可用时降级为 TTL 兜底）。
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.mcp_client import (
    AUTH_BEARER,
    AUTH_HEADER,
    AUTH_NONE,
    TRANSPORT_AUTO,
    TRANSPORT_LEGACY_REST,
    TRANSPORT_STREAMABLE_HTTP,
    MCPRemoteClient,
    MCPServerSpec,
    spec_from_config,
)
from app.agent.tools.structured_result import (
    StructuredResultValidationError,
    normalize_output_schema,
)
from app.api.deps import require_platform
from app.auth.secret_box import encrypt, mask
from app.mcp.url_guard import UnsafeMcpUrlError, validate_mcp_url
from app.schema.db import MCPConfig
from app.storage.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/mcp-configs",
    tags=["MCP Config"],
    dependencies=[Depends(require_platform())],
)

_VALID_TRANSPORTS = {TRANSPORT_AUTO, TRANSPORT_STREAMABLE_HTTP, TRANSPORT_LEGACY_REST}
_VALID_AUTH_TYPES = {AUTH_NONE, AUTH_BEARER, AUTH_HEADER}


class MCPConfigCreate(BaseModel):
    """创建 MCP server 配置请求"""
    name: str
    url: str
    enabled: bool = True
    transport: str = TRANSPORT_AUTO
    auth_type: str = AUTH_NONE
    # 明文凭据，仅入参；落库前加密，响应不回显
    auth_token: Optional[str] = None
    auth_header_name: Optional[str] = None
    forward_context: bool = False
    tool_prefix: Optional[str] = None


class MCPConfigUpdate(BaseModel):
    """更新 MCP server 配置请求（所有字段 Optional，未传即不改）

    ``auth_token`` 的三态语义：不传 = 保持原凭据；传空串 = 清除凭据；传非空 = 换新凭据。
    这是"写入型敏感字段 + 响应不回显"场景的常规约定，避免前端因拿不到原值而误清。
    """
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None
    transport: Optional[str] = None
    auth_type: Optional[str] = None
    auth_token: Optional[str] = None
    auth_header_name: Optional[str] = None
    forward_context: Optional[bool] = None
    tool_prefix: Optional[str] = None


class MCPTestRequest(BaseModel):
    """连通性测试请求（对未保存的配置）"""
    url: str
    transport: str = TRANSPORT_AUTO
    auth_type: str = AUTH_NONE
    auth_token: Optional[str] = None
    auth_header_name: Optional[str] = None


class MCPToolMeta(BaseModel):
    """远端 MCP server 暴露的单个工具元信息"""
    name: str
    description: str = ""
    # 标准 MCP 可选声明；仅用于观测，实际调用校验在 MCPToolWrapper 执行期完成。
    output_schema: Optional[dict] = None


class MCPTestResponse(BaseModel):
    """连通性测试结果

    ``protocol`` 回显本次实际走通的传输方式（``streamable_http`` = 标准 MCP，
    ``legacy_rest`` = 老的私有 REST），便于判断第三方是否已完成协议升级。
    """
    reachable: bool
    tool_count: int = 0
    tools: list[MCPToolMeta] = []
    protocol: Optional[str] = None
    error: Optional[str] = None


class MCPConfigResponse(BaseModel):
    """MCP server 配置响应（凭据只回掩码）"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    url: str
    enabled: bool
    transport: str
    auth_type: str
    auth_header_name: Optional[str] = None
    has_auth_token: bool = False
    auth_token_masked: Optional[str] = None
    forward_context: bool = False
    tool_prefix: Optional[str] = None
    created_at: str
    updated_at: str


def _config_to_response(config: MCPConfig) -> MCPConfigResponse:
    from app.auth.secret_box import decrypt

    stored = config.auth_token_encrypted
    return MCPConfigResponse(
        id=config.id,
        name=config.name,
        url=config.url,
        enabled=config.enabled,
        transport=config.transport or TRANSPORT_AUTO,
        auth_type=config.auth_type or AUTH_NONE,
        auth_header_name=config.auth_header_name,
        has_auth_token=bool(stored),
        # 掩码基于解密后的明文长度/尾部，仅用于"确认配的是哪一把"，无法据此还原
        auth_token_masked=mask(decrypt(stored)) if stored else None,
        forward_context=bool(config.forward_context),
        tool_prefix=config.tool_prefix,
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


def _normalize_url(url: str) -> str:
    """规整并校验 url：去空白/尾斜杠 + SSRF 护栏（含域名解析检查）。"""
    try:
        return validate_mcp_url(url, resolve=True)
    except UnsafeMcpUrlError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _tool_meta(tool: dict) -> MCPToolMeta | None:
    name = tool.get("name")
    if not name:
        return None
    output_schema = None
    if tool.get("outputSchema") is not None:
        try:
            output_schema = normalize_output_schema(tool.get("outputSchema"))
        except StructuredResultValidationError as exc:
            logger.warning("[MCP] ignored invalid outputSchema for tool '%s': %s", name, exc)
    return MCPToolMeta(
        name=name,
        description=tool.get("description", ""),
        output_schema=output_schema,
    )


def _validate_enums(transport: str | None, auth_type: str | None) -> None:
    if transport is not None and transport not in _VALID_TRANSPORTS:
        raise HTTPException(
            status_code=422, detail=f"transport 非法，可选：{sorted(_VALID_TRANSPORTS)}"
        )
    if auth_type is not None and auth_type not in _VALID_AUTH_TYPES:
        raise HTTPException(
            status_code=422, detail=f"auth_type 非法，可选：{sorted(_VALID_AUTH_TYPES)}"
        )


def _validate_auth_shape(auth_type: str, has_token: bool, header_name: str | None) -> None:
    """凭据字段自洽性校验：选了认证方式就必须给齐必需项，避免"以为配了其实没带"。"""
    if auth_type == AUTH_NONE:
        return
    if not has_token:
        raise HTTPException(status_code=422, detail="选择了认证方式时必须提供 auth_token")
    if auth_type == AUTH_HEADER and not (header_name or "").strip():
        raise HTTPException(
            status_code=422, detail="auth_type=header 时必须提供 auth_header_name"
        )


async def _apply_mcp_config_change(db: AsyncSession) -> None:
    """让 MCP 配置变更立即生效（提交事务 + 广播各进程失效缓存）

    先提交再广播：其他进程收到信号后重新查库发现，未提交则读到旧数据。
    本进程经 reload_capability_locally("mcp") 立即失效本地缓存（工具发现 + 传输探测
    + 握手会话，三者都会因 url/transport/凭据变更而失效）。
    """
    from app.api.capability_reload import CAPABILITY_MCP, apply_and_broadcast

    await db.commit()
    await apply_and_broadcast(CAPABILITY_MCP)


async def _perform_mcp_test(spec: MCPServerSpec) -> MCPTestResponse:
    """连通性测试：按 spec（含凭据/传输模式）拉取工具列表并回显结果。"""
    client = MCPRemoteClient(spec)
    try:
        tools = await client.list_tools()
    except Exception as e:
        logger.warning("[MCP] Connectivity test failed for %s: %s", spec.url, e)
        return MCPTestResponse(reachable=False, error=str(e), protocol=client.last_transport)
    return MCPTestResponse(
        reachable=True,
        tool_count=len(tools),
        tools=[meta for tool in tools if (meta := _tool_meta(tool)) is not None],
        protocol=client.last_transport,
    )


@router.get("", response_model=list[MCPConfigResponse])
async def list_mcp_configs(db: AsyncSession = Depends(get_db)):
    """获取全部 MCP server 配置，按创建时间倒序"""
    result = await db.execute(select(MCPConfig).order_by(MCPConfig.created_at.desc()))
    return [_config_to_response(c) for c in result.scalars().all()]


@router.post("", response_model=MCPConfigResponse, status_code=201)
async def create_mcp_config(body: MCPConfigCreate, db: AsyncSession = Depends(get_db)):
    """创建 MCP server 配置"""
    url = _normalize_url(body.url)
    _validate_enums(body.transport, body.auth_type)
    _validate_auth_shape(body.auth_type, bool(body.auth_token), body.auth_header_name)

    config = MCPConfig(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        url=url,
        enabled=body.enabled,
        transport=body.transport,
        auth_type=body.auth_type,
        auth_token_encrypted=encrypt(body.auth_token),
        auth_header_name=(body.auth_header_name or "").strip() or None,
        forward_context=body.forward_context,
        tool_prefix=(body.tool_prefix or "").strip() or None,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)

    response = _config_to_response(config)
    await _apply_mcp_config_change(db)
    return response


@router.put("/{config_id}", response_model=MCPConfigResponse)
async def update_mcp_config(config_id: str, body: MCPConfigUpdate, db: AsyncSession = Depends(get_db)):
    """更新 MCP server 配置"""
    config = await db.get(MCPConfig, config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="MCP 配置不存在")

    _validate_enums(body.transport, body.auth_type)

    if body.name is not None:
        config.name = body.name.strip()
    if body.url is not None:
        config.url = _normalize_url(body.url)
    if body.enabled is not None:
        config.enabled = body.enabled
    if body.transport is not None:
        config.transport = body.transport
    if body.auth_type is not None:
        config.auth_type = body.auth_type
    if body.auth_header_name is not None:
        config.auth_header_name = body.auth_header_name.strip() or None
    if body.forward_context is not None:
        config.forward_context = body.forward_context
    if body.tool_prefix is not None:
        config.tool_prefix = body.tool_prefix.strip() or None
    # 凭据三态：None=不改，""=清除，非空=换新
    if body.auth_token is not None:
        config.auth_token_encrypted = encrypt(body.auth_token) if body.auth_token else None

    _validate_auth_shape(
        config.auth_type or AUTH_NONE,
        bool(config.auth_token_encrypted),
        config.auth_header_name,
    )

    await db.flush()
    await db.refresh(config)

    response = _config_to_response(config)
    await _apply_mcp_config_change(db)
    return response


@router.delete("/{config_id}", status_code=204)
async def delete_mcp_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除 MCP server 配置"""
    config = await db.get(MCPConfig, config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="MCP 配置不存在")

    await db.delete(config)
    await _apply_mcp_config_change(db)


@router.post("/test", response_model=MCPTestResponse)
async def test_mcp_connection(body: MCPTestRequest):
    """对未保存的配置做连通性测试（保存前先验证，可带待用的凭据）"""
    _validate_enums(body.transport, body.auth_type)
    spec = MCPServerSpec(
        id="",
        name=body.url,
        url=_normalize_url(body.url),
        transport=body.transport,
        auth_type=body.auth_type,
        auth_token=body.auth_token,
        auth_header_name=body.auth_header_name,
    )
    return await _perform_mcp_test(spec)


@router.post("/{config_id}/test", response_model=MCPTestResponse)
async def test_saved_mcp_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """对已保存配置做连通性测试（使用其真实凭据）"""
    config = await db.get(MCPConfig, config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="MCP 配置不存在")
    return await _perform_mcp_test(spec_from_config(config))


@router.get("/{config_id}/tools", response_model=list[MCPToolMeta])
async def list_mcp_tools(config_id: str, db: AsyncSession = Depends(get_db)):
    """列出已保存配置暴露的工具（供前端查看/确认注入内容）"""
    config = await db.get(MCPConfig, config_id)
    if config is None:
        raise HTTPException(status_code=404, detail="MCP 配置不存在")
    try:
        tools = await MCPRemoteClient(spec_from_config(config)).list_tools()
    except Exception as e:
        logger.warning("[MCP] Fetch tools failed for %s: %s", config.url, e)
        raise HTTPException(status_code=400, detail=f"获取工具列表失败: {e}")
    return [meta for tool in tools if (meta := _tool_meta(tool)) is not None]
