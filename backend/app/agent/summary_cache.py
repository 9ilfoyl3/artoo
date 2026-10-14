"""Redis-backed document summary cache for generic map-reduce summarization."""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

KEY_PREFIX = "agent:summary"
DEFAULT_TTL = 1800


class SummaryCache:
    def __init__(self, redis_client, ttl: int = DEFAULT_TTL):
        self._redis = redis_client
        self._ttl = ttl

    @staticmethod
    def build_key(
        doc_id: str,
        version_hash: str,
        model_key: str,
        cache_scope: str,
    ) -> str:
        raw = "\0".join([doc_id, version_hash, model_key, cache_scope])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"{KEY_PREFIX}:{doc_id}:{digest}"

    async def get(self, key: str) -> str | None:
        if not self._redis:
            return None
        try:
            return await self._redis.get(key)
        except Exception as exc:  # noqa: BLE001 - cache is best-effort
            logger.warning("summary cache read failed: %s", exc)
            return None

    async def set(self, key: str, summary: str) -> None:
        if not self._redis or not summary:
            return
        try:
            await self._redis.setex(key, self._ttl, summary)
        except Exception as exc:  # noqa: BLE001 - cache is best-effort
            logger.warning("summary cache write failed: %s", exc)


_cache_instance: SummaryCache | None = None


async def get_summary_cache() -> SummaryCache | None:
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance
    try:
        import redis.asyncio as aioredis

        from app.config import get_settings

        settings = get_settings()
        redis_url = getattr(settings, "redis_url", "")
        if not redis_url:
            return None
        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
        _cache_instance = SummaryCache(
            client,
            ttl=getattr(settings, "retrieval_cache_ttl", DEFAULT_TTL),
        )
        return _cache_instance
    except Exception as exc:  # noqa: BLE001 - disable cache on infrastructure failure
        logger.info("summary cache disabled: %s", exc)
        return None
