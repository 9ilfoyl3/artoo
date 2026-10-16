"""Pipeline 工厂函数

提供创建 DocumentPipeline 实例的统一入口，
避免 API 和 Worker 各自手动组装依赖。
"""

import logging

from app.config import get_settings
from app.models.manager import get_model_manager
from app.pipeline.pipeline import DocumentPipeline
from app.startup import load_asr_manager, load_ocr_manager
from app.storage.database import async_session
from app.storage.milvus import get_milvus_client

logger = logging.getLogger(__name__)


async def create_pipeline() -> DocumentPipeline:
    """创建完整的 DocumentPipeline 实例（含所有依赖）

    Returns:
        配置完整的 DocumentPipeline 实例
    """
    model_manager = get_model_manager()
    milvus_client = get_milvus_client()
    ocr_manager = await load_ocr_manager()
    asr_manager = await load_asr_manager()

    # 知识图谱抽取慢道队列：仅全局开关开启时才创建（关闭时不连 Redis，零额外成本，Req 9.3）。
    # Redis 不可用时 create_graph_queue 返回 None，文档完成后跳过图谱触发（优雅降级）。
    graph_queue = None
    settings = get_settings()
    if settings.graph_enable:
        from app.pipeline.graph.trigger import create_graph_queue

        graph_queue = await create_graph_queue(settings.redis_url)
        if graph_queue is None:
            logger.warning("GRAPH_ENABLE=true 但 pipeline:graph 慢道队列创建失败（Redis 不可用），图谱抽取将不触发")

    from app.summary.queue import get_document_summary_queue

    return DocumentPipeline(
        model_manager=model_manager,
        milvus_client=milvus_client,
        db_session_factory=async_session,
        ocr_manager=ocr_manager,
        asr_manager=asr_manager,
        graph_queue=graph_queue,
        summary_queue=get_document_summary_queue(),
    )
