"""OCR Manager - Provider 注册、选择、能力编排与 fallback"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .errors import OCRError
from .input_prep import PreparedInput, prepare_input
from .provider import OCRProvider, OCRResult, PageOCRResult
from .registry import get_provider_class

if TYPE_CHECKING:
    from app.schema.db import OCRConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ProviderState:
    """Provider 集合与选路信息的不可变快照

    热重载时整体替换本对象（单次引用赋值），避免出现"providers 已换、
    default_name 还是旧的"这类半更新中间态。
    """

    providers: dict[str, OCRProvider] = field(default_factory=dict)
    default_name: str = ""
    fallback_name: str = ""


class OCRManager:
    """OCR Provider 管理器

    职责：从数据库配置注册 Provider、按 default/fallback 选择、
    依据 Provider 能力编排输入（整文件直送 / 按页图片逐页识别）。
    不涉及任何响应格式判断——那是各 Provider 的契约。

    支持配置热重载：:meth:`reload_from_configs` 原子替换内部 Provider 集合，
    Manager 实例对象本身不变，因此所有持有该实例引用的位置
    （如 ``DocumentPipeline.ocr_manager``）自动同步生效，无需重建 pipeline。
    """

    def __init__(self, configs: list[OCRConfig]) -> None:
        """初始化 OCR Manager

        Args:
            configs: 数据库中的 OCR 配置列表（可为空列表，此时 Manager 视为未配置）
        """
        self._state = self._build_state(configs)

    @classmethod
    def _build_state(cls, configs: list[OCRConfig]) -> _ProviderState:
        """按数据库配置构建 Provider 集合快照（不触碰实例状态）

        仅注册 provider_type 在注册表内、且 is_available() 为 True 的配置；
        单条配置构建失败只跳过该条，不影响其余配置。

        Args:
            configs: 数据库中的 OCR 配置列表

        Returns:
            构建好的 :class:`_ProviderState` 快照
        """
        logger.info("[OCR] 开始构建 OCR Provider 集合, 配置数量: %d", len(configs))

        providers: dict[str, OCRProvider] = {}
        default_name = ""
        fallback_name = ""

        for config in configs:
            provider = cls._create_provider(config)
            if provider and provider.is_available():
                providers[config.id] = provider
                logger.info("[OCR] 已注册 Provider: %s (id=%s)", provider.name, config.id)

                if config.is_default:
                    default_name = config.id
                if config.is_fallback:
                    fallback_name = config.id
            else:
                logger.warning(
                    "[OCR] Provider '%s' (id=%s) 不可用或类型未注册，跳过",
                    config.provider_type, config.id,
                )

        logger.info(
            "[OCR] 构建完成，已注册 %d 个 Provider, 默认: %s, Fallback: %s",
            len(providers),
            default_name or "(无)",
            fallback_name or "(无)",
        )
        return _ProviderState(
            providers=providers, default_name=default_name, fallback_name=fallback_name
        )

    def reload_from_configs(self, configs: list[OCRConfig]) -> None:
        """按新配置热重载 Provider 集合（原子替换，无需重启进程）

        先完整构建新快照，成功后再单次赋值替换；构建期抛异常则保留原有配置继续
        服务（宁可用旧配置，也不让 Manager 处于不可用状态）。

        正在执行中的识别调用已解析到旧 Provider 对象，继续跑完不受影响；
        后续新任务经 :meth:`get_provider` 拿到新集合。

        Args:
            configs: 数据库中的最新 OCR 配置列表
        """
        try:
            new_state = self._build_state(configs)
        except Exception as e:  # noqa: BLE001 — 重载失败不能打断既有服务
            logger.warning("[OCR] 配置热重载失败，保留原有配置: %s", e)
            return

        self._state = new_state
        logger.info(
            "[OCR] 配置热重载完成，当前 %d 个 Provider 可用", len(new_state.providers)
        )

    def has_provider(self) -> bool:
        """是否存在可用 Provider（即该能力是否真正可用）"""
        return bool(self._state.providers)

    def __bool__(self) -> bool:
        """无可用 Provider 的 Manager 语义上等价于"未配置 OCR"。

        使调用方既有的 ``if self.ocr_manager`` 真值判断保持原语义：启动时数据库无
        配置也会创建空 Manager（为"首次配置后热生效"留下可重载的对象），此时真值
        为 False，pipeline 行为与过去 ``ocr_manager is None`` 完全一致。
        """
        return self.has_provider()

    @staticmethod
    def _create_provider(config: OCRConfig) -> OCRProvider | None:
        """按注册表创建 Provider 实例

        Args:
            config: 单条 OCR 配置记录

        Returns:
            OCRProvider 实例，或 None（provider_type 未注册）
        """
        # 导入 providers 包触发注册（放在此处避免模块级循环导入）
        import app.pipeline.ocr.providers  # noqa: F401

        provider_cls = get_provider_class(config.provider_type)
        if provider_cls is None:
            logger.warning(
                "[OCR] 不支持的 provider_type: %s（该配置需重建）", config.provider_type
            )
            return None

        return provider_cls(  # type: ignore[call-arg]
            api_url=config.api_url,
            api_key=config.api_key or "",
            timeout=config.timeout,
            extra_config=config.extra_config or {},
        )

    def get_provider(self, name: Optional[str] = None) -> OCRProvider:
        """获取指定 Provider，默认返回配置中的默认 Provider

        Args:
            name: Provider ID（config.id），为 None 时使用默认 Provider

        Returns:
            OCRProvider: 对应的 Provider 实例

        Raises:
            ValueError: 指定的 Provider 未注册或不可用
        """
        state = self._state
        target_name = name if name is not None else state.default_name

        if target_name not in state.providers:
            available = list(state.providers.keys())
            raise ValueError(
                f"OCR Provider '{target_name}' 未注册或不可用，当前可用: {available}"
            )

        return state.providers[target_name]

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider ID"""
        return list(self._state.providers.keys())

    async def recognize(
        self, file_path: str, provider_name: Optional[str] = None
    ) -> OCRResult:
        """单文件直送识别（文件须为目标 Provider 可接受的类型）

        用于嵌入图片 OCR 与配置连通性测试。整篇文档识别请用
        :meth:`recognize_document`，它会按 Provider 能力准备输入。

        Args:
            file_path: 文件路径
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider

        Returns:
            OCRResult: 统一格式的识别结果
        """
        provider = self.get_provider(provider_name)
        from app.pipeline.concurrency import get_ocr_semaphore

        async with get_ocr_semaphore():
            return await provider.recognize(file_path)

    async def recognize_document(
        self,
        file_path: str,
        provider_name: Optional[str] = None,
        page_numbers: Optional[list[int]] = None,
    ) -> OCRResult:
        """整篇文档识别：按 Provider 能力准备输入，失败时切换 fallback

        - Provider 接受该输入类型 → 整文件直送。
        - 只接受图片而输入是 PDF → 按页渲染整页图片并发逐页识别，按页合并。

        Args:
            file_path: 文件路径（PDF / 图片）
            provider_name: 指定 Provider ID，为 None 时使用默认 Provider
            page_numbers: 仅识别 PDF 的指定页码（从 1 开始）；None 表示全部页面。

        Returns:
            OCRResult: 统一格式的识别结果

        Raises:
            ValueError: 无可用 Provider
            OCRError: 识别失败且无可用 fallback（含契约不符、输入不支持）
        """
        primary_name = provider_name or self._state.default_name
        primary = self.get_provider(provider_name)
        logger.info(
            "[OCR] 开始识别文档: %s, Provider: %s (accepts=%s)",
            file_path, primary.name, sorted(primary.capability.accepts),
        )

        try:
            return await self._run_with_capability(
                primary, file_path, page_numbers=page_numbers
            )
        except Exception as primary_error:
            logger.warning(
                "[OCR] Provider '%s' 识别失败: %s: %s",
                primary.name, type(primary_error).__name__, primary_error,
            )

            fallback = self._pick_fallback(primary_name)
            if fallback is None:
                raise

            logger.info("[OCR] 切换到 fallback Provider: %s", fallback.name)
            try:
                # fallback 能力可能与 primary 不同，重新做输入准备
                return await self._run_with_capability(
                    fallback, file_path, page_numbers=page_numbers
                )
            except Exception as fallback_error:
                logger.error(
                    "[OCR] Fallback Provider '%s' 也失败: %s",
                    fallback.name, fallback_error,
                )
                raise primary_error from fallback_error

    def _pick_fallback(self, primary_name: str) -> OCRProvider | None:
        """取可用的 fallback Provider（与 primary 不同才有意义）"""
        state = self._state
        if (
            state.fallback_name
            and state.fallback_name in state.providers
            and state.fallback_name != primary_name
        ):
            return state.providers[state.fallback_name]
        return None

    async def _run_with_capability(
        self,
        provider: OCRProvider,
        file_path: str,
        page_numbers: Optional[list[int]] = None,
    ) -> OCRResult:
        """按能力准备输入并执行识别，最后清理临时产物"""
        prepared = await asyncio.to_thread(
            prepare_input,
            file_path,
            provider.capability,
            provider.name,
            page_numbers,
        )
        try:
            if prepared.kind == "whole":
                return await self._recognize_one(provider, prepared.paths[0])
            return await self._recognize_pages(provider, prepared)
        finally:
            prepared.cleanup()

    @staticmethod
    async def _recognize_one(provider: OCRProvider, path: str) -> OCRResult:
        """单次识别，受全局 OCR 信号量限流"""
        from app.pipeline.concurrency import get_ocr_semaphore

        async with get_ocr_semaphore():
            return await provider.recognize(path)

    async def _recognize_pages(
        self, provider: OCRProvider, prepared: PreparedInput
    ) -> OCRResult:
        """并发逐页识别并按页码合并为单个 OCRResult

        单页失败不放弃整篇：记 WARNING 后该页留空，其余页正常合并
        （扫描件常有个别页因图像质量导致服务报错）。全部页失败则抛出。
        """
        from app.pipeline.concurrency import get_ocr_semaphore

        semaphore = get_ocr_semaphore()
        total = len(prepared.paths)

        async def _one(idx: int, path: str) -> OCRResult | Exception:
            async with semaphore:
                try:
                    return await provider.recognize(path)
                except Exception as e:  # noqa: BLE001 — 单页失败降级为空页
                    logger.warning(
                        "[OCR] 第 %d/%d 页识别失败: %s: %s",
                        idx + 1, total, type(e).__name__, e,
                    )
                    return e

        results = await asyncio.gather(
            *[_one(i, p) for i, p in enumerate(prepared.paths)]
        )

        pages: list[PageOCRResult] = []
        all_conf: list[float] = []
        errors: list[Exception] = []
        for idx, res in enumerate(results):
            page_num = (
                prepared.page_numbers[idx]
                if idx < len(prepared.page_numbers)
                else idx + 1
            )
            if isinstance(res, Exception):
                errors.append(res)
                pages.append(PageOCRResult(page_num=page_num, blocks=[], full_text=""))
                continue
            # 单图识别的 provider 端页码恒为 1，此处按渲染页序重编号
            page_text = res.full_text
            page_blocks = res.pages[0].blocks if res.pages else []
            pages.append(
                PageOCRResult(page_num=page_num, blocks=page_blocks, full_text=page_text)
            )
            if res.avg_confidence:
                all_conf.append(res.avg_confidence)

        if errors and len(errors) == total:
            # 每一页都失败：不是"内容为空"而是链路故障，抛出首个错误
            raise errors[0]

        full_text = "\n\n".join(p.full_text for p in pages if p.full_text)
        avg_conf = sum(all_conf) / len(all_conf) if all_conf else 0.0
        logger.info(
            "[OCR] 逐页识别完成: %d 页（失败 %d 页），文本长度=%d",
            total, len(errors), len(full_text),
        )
        return OCRResult(
            full_text=full_text,
            pages=pages,
            avg_confidence=avg_conf,
            provider_name=provider.name,
            metadata={
                "ocr_page_count": total,
                "ocr_failed_pages": len(errors),
                "ocr_truncated_pages": prepared.truncated_pages,
                "ocr_page_numbers": prepared.page_numbers,
            },
        )


__all__ = ["OCRManager", "OCRError"]
