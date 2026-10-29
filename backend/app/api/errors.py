"""统一异常与全局异常处理（tenant-auth）。

对齐团队规范"Controller 层禁止 try-catch，由 GlobalExceptionHandler 统一处理"的精神：
鉴权相关异常由 Guard / 授权判定函数抛出，路由层不再各自 try-catch 兜鉴权。

状态码语义（安全设计的一部分，三处收敛点必须一致）：
- 401 未认证：无凭据 / 凭据无效 / JWT 过期或签名错 / Key 不存在或被撤销
- 404 跨租户 / 不在可读范围：**存在性非泄露**，与"资源不存在"不可区分
- 403 已认证但权限不足 / 租户或用户停用 / must_change_password 闸门 / api_key 触达管理或平台操作
- 400 输入非法：非法 grantee_type 预留值 / 代理 Key 缺 X-External-User-Id
"""

from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ErrorCodeEnum(str, Enum):
    """业务错误码（禁止硬编码字符串/数字散落各处）。"""

    UNAUTHENTICATED = "unauthenticated"
    CROSS_TENANT = "cross_tenant"          # 对外表现为 404
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    TENANT_DISABLED = "tenant_disabled"
    USER_DISABLED = "user_disabled"
    MUST_CHANGE_PASSWORD = "must_change_password"
    INVALID_GRANTEE_TYPE = "invalid_grantee_type"
    MISSING_EXTERNAL_USER_ID = "missing_external_user_id"
    VALIDATION_ERROR = "validation_error"
    BAD_REQUEST = "bad_request"
    FILE_TOO_LARGE = "file_too_large"
    EMPTY_DOCUMENT_CONTENT = "empty_document_content"


class AppError(Exception):
    """业务异常基类。携带错误码与 HTTP 状态码，由全局 handler 统一转响应。"""

    code: ErrorCodeEnum = ErrorCodeEnum.BAD_REQUEST
    http_status: int = 400
    # 默认对外文案。跨租户/不存在必须使用**相同**文案以保证存在性非泄露。
    default_detail: str = "请求无法处理"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class UnauthenticatedError(AppError):
    code = ErrorCodeEnum.UNAUTHENTICATED
    http_status = 401
    default_detail = "未认证或凭据无效"


class CrossTenantError(AppError):
    """跨租户访问 / 资源不存在 / 不在可读范围——统一 404 且文案一致（存在性非泄露）。"""

    code = ErrorCodeEnum.CROSS_TENANT
    http_status = 404
    default_detail = "资源不存在"


class NotFoundError(AppError):
    """普通不存在。文案与 CrossTenantError 保持一致，攻击者无法区分。"""

    code = ErrorCodeEnum.NOT_FOUND
    http_status = 404
    default_detail = "资源不存在"


class PermissionDeniedError(AppError):
    code = ErrorCodeEnum.PERMISSION_DENIED
    http_status = 403
    default_detail = "权限不足"


class TenantDisabledError(AppError):
    code = ErrorCodeEnum.TENANT_DISABLED
    http_status = 403
    default_detail = "租户已停用"


class UserDisabledError(AppError):
    code = ErrorCodeEnum.USER_DISABLED
    http_status = 403
    default_detail = "账号已停用"


class MustChangePasswordError(AppError):
    code = ErrorCodeEnum.MUST_CHANGE_PASSWORD
    http_status = 403
    default_detail = "请先修改初始口令后再操作"


class InvalidGranteeTypeError(AppError):
    code = ErrorCodeEnum.INVALID_GRANTEE_TYPE
    http_status = 400
    default_detail = "不支持的被授权主体类型"


class MissingExternalUserIdError(AppError):
    code = ErrorCodeEnum.MISSING_EXTERNAL_USER_ID
    http_status = 400
    default_detail = "缺少 X-External-User-Id 请求头"


class ValidationInputError(AppError):
    """输入校验失败（用户名/口令/名称不合规）-> 400。"""

    code = ErrorCodeEnum.VALIDATION_ERROR
    http_status = 400
    default_detail = "输入不合法"


class FileTooLargeError(AppError):
    """上传文件超过租户级 Upload_File_Size_Limit（session-file-upload，Req 3.2）。

    在上传入口（解析前）按 ``UploadLimitResolver`` 求得的生效上限拦截，返回 413 并
    在文案中带上允许的大小上限（MB），供前端明确提示。会话上传与知识库上传共用同一上限。
    """

    code = ErrorCodeEnum.FILE_TOO_LARGE
    http_status = 413
    default_detail = "上传文件超过允许的大小上限"

    @classmethod
    def from_limit(cls, limit_bytes: int) -> "FileTooLargeError":
        """据生效字节上限构造异常，文案带上允许的 MB 上限（Req 3.2）。"""
        limit_mb = limit_bytes // (1024 * 1024)
        return cls(f"上传文件超过允许的大小上限（最大 {limit_mb}MB）")


class EmptyDocumentContentError(AppError):
    """文档缺少建立索引所需的处理结果 -> 422。

    显式空文档会以 ``completed`` + ``chunk_count=0`` 正常完成，不再使用本异常。
    当前场景仅限无法执行必要处理（如音频需要 ASR 但未配置）以及非空文档意外切出零
    child chunk。此为用户输入或配置问题而非服务端故障，应优雅提示而非 500。
    """

    code = ErrorCodeEnum.EMPTY_DOCUMENT_CONTENT
    http_status = 422
    default_detail = "文档无可提取内容，无法建立索引（请确认文件包含可识别的文本）"


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理：把 AppError 统一转为 {"detail": ...} 响应。

    容量闸门业务异常 ``UploadCapExceeded``（在 ``app.pipeline.pipeline``，同时被 KB 异步
    路径以"置文档 failed"方式吞掉）会从会话同步上传路径透出到 HTTP 层，统一在此映射为
    413 Payload Too Large，文案直接取异常消息（已含 used/incoming/cap 信息），保证路由层
    零 try/catch（团队规范"Controller 层禁止 try-catch"）。延迟导入避免循环依赖。
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.detail})

    from app.pipeline.pipeline import UploadCapExceeded

    @app.exception_handler(UploadCapExceeded)
    async def _handle_upload_cap_exceeded(
        _request: Request, exc: UploadCapExceeded
    ) -> JSONResponse:
        return JSONResponse(status_code=413, content={"detail": str(exc)})
