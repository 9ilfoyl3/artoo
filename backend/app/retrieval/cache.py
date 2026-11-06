"""检索结果缓存

基于 Redis 的检索结果缓存，减少重复查询的检索+Rerank 开销。
- 缓存 key: rag:cache:{kb_id}:{hash(query+mode)}
- TTL: 30 分钟（可配置）
- 文档变更时主动清除该知识库缓存
"""

import hashlib
import json
import logging
from typing import Optional

from app.retrieval.base import RetrievalResult

logger = logging.getLogger(__name__)

# 默认 TTL（秒）
DEFAULT_TTL = 1800  # 30 分钟

# Redis key 前缀
KEY_PREFIX = "rag:cache"


class RetrievalCache:
    """检索结果缓存管理器"""

    def __init__(self, redis_client, ttl: int = DEFAULT_TTL):
        """初始化缓存

        Args:
            redis_client: aioredis 客户端实例
            ttl: 缓存过期时间（秒），默认 30 分钟
        """
        self._redis = redis_client
        self._ttl = ttl

    @staticmethod
    def _build_key(
        kb_id: str,
        query: str,
        mode: str,
        *,
        scope: str = "",
        top_k: int | None = None,
        tenant_id: str | None = None,
        config_version: str = "",
    ) -> str:
        """构建缓存 key

        格式: rag:cache:{kb_id}:{hash}
        hash 基于 query + mode 生成，16 位足够避免碰撞
        """
        raw = "\0".join([
            query,
            mode,
            scope,
            "" if top_k is None else str(top_k),
            tenant_id or "",
            config_version,
        ])
        hash_val = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"{KEY_PREFIX}:{kb_id}:{hash_val}"

    async def get(
        self,
        kb_id: str,
        query: str,
        mode: str,
        *,
        scope: str = "",
        top_k: int | None = None,
        tenant_id: str | None = None,
        config_version: str = "",
    ) -> Optional[list[RetrievalResult]]:
        """获取缓存的检索结果

        Args:
            kb_id: 知识库 ID
            query: 查询文本
            mode: 检索模式（direct/hybrid/agent）

        Returns:
            缓存命中返回 list[RetrievalResult]，未命中返回 None
        """
        if not self._redis:
            return None

        key = self._build_key(
            kb_id,
            query,
            mode,
            scope=scope,
            top_k=top_k,
            tenant_id=tenant_id,
            config_version=config_version,
        )
        try:
            data = await self._redis.get(key)
            if data is None:
                return None

            items = json.loads(data)
            results = [
                RetrievalResult(
                    chunk_id=item["chunk_id"],
                    content=item["content"],
                    score=item["score"],
                    doc_id=item["doc_id"],
                    metadata=item.get("metadata", {}),
                    child_content=item.get("child_content", ""),
                )
                for item in items
            ]
            logger.debug("缓存命中: kb=%s, query=%s, mode=%s, 结果数=%d", kb_id, query[:30], mode, len(results))
            return results

        except Exception as e:
            logger.warning("读取缓存失败（降级为无缓存）: %s", e)
            return None

    async def set(
        self,
        kb_id: str,
        query: str,
        mode: str,
        results: list[RetrievalResult],
        *,
        scope: str = "",
        top_k: int | None = None,
        tenant_id: str | None = None,
        config_version: str = "",
    ) -> None:
        """写入检索结果缓存

        Args:
            kb_id: 知识库 ID
            query: 查询文本
            mode: 检索模式
            results: 检索结果列表
        """
        if not self._redis or not results:
            return

        key = self._build_key(
            kb_id,
            query,
            mode,
            scope=scope,
            top_k=top_k,
            tenant_id=tenant_id,
            config_version=config_version,
        )
        try:
            items = [
                {
                    "chunk_id": r.chunk_id,
                    "content": r.content,
                    "score": r.score,
                    "doc_id": r.doc_id,
                    "metadata": r.metadata,
                    "child_content": r.child_content or "",
                }
                for r in results
            ]
            await self._redis.setex(key, self._ttl, json.dumps(items, ensure_ascii=False))
            logger.debug("缓存写入: kb=%s, query=%s, mode=%s, 结果数=%d", kb_id, query[:30], mode, len(results))

        except Exception as e:
            logger.warning("写入缓存失败（不影响正常流程）: %s", e)

    async def invalidate_kb(self, kb_id: str) -> int:
        """清除指定知识库的所有缓存

        文档上传/删除/更新时调用。

        Args:
            kb_id: 知识库 ID

        Returns:
            清除的 key 数量
        """
        if not self._redis:
            return 0

        pattern = f"{KEY_PREFIX}:{kb_id}:*"
        count = 0
        try:
            async for key in self._redis.scan_iter(match=pattern):
                await self._redis.delete(key)
                count += 1
            if count > 0:
                print(f"[Cache] 清除: kb={kb_id}, 清除 {count} 条缓存")
                logger.info("清除知识库缓存: kb=%s, 清除 %d 条", kb_id, count)
            return count

        except Exception as e:
            logger.warning("清除缓存失败: %s", e)
            return 0

    async def invalidate_all(self) -> int:
        """清除所有检索缓存（慎用）"""
        if not self._redis:
            return 0

        pattern = f"{KEY_PREFIX}:*"
        count = 0
        try:
            async for key in self._redis.scan_iter(match=pattern):
                await self._redis.delete(key)
                count += 1
            logger.info("清除全部检索缓存: %d 条", count)
            return count

        except Exception as e:
            logger.warning("清除全部缓存失败: %s", e)
            return 0


# ============================================================
# 全局缓存实例管理
# ============================================================

_cache_instance: Optional[RetrievalCache] = None


async def get_retrieval_cache() -> Optional[RetrievalCache]:
    """获取全局缓存实例

    如果 Redis 未配置或连接失败，返回 None（系统降级为无缓存模式）。
    """
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance

    try:
        import redis.asyncio as aioredis
        from app.config import get_settings

        settings = get_settings()
        redis_url = getattr(settings, "redis_url", "")

        if not redis_url:
            logger.info("Redis 未配置，检索缓存禁用")
            return None

        client = aioredis.from_url(redis_url, decode_responses=True)
        # 测试连接
        await client.ping()

        ttl = getattr(settings, "retrieval_cache_ttl", DEFAULT_TTL)
        _cache_instance = RetrievalCache(client, ttl=ttl)
        logger.info("检索缓存已启用: redis=%s, ttl=%ds", redis_url, ttl)
        return _cache_instance

    except Exception as e:
        logger.info("Redis 连接失败，检索缓存禁用: %s", e)
        return None
