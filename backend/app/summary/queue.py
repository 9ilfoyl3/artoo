"""Redis Stream queue for durable document-summary generation."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis.asyncio as aioredis

from app.pipeline.stream_queue import RedisStreamQueue

logger = logging.getLogger(__name__)

SUMMARY_STREAM_KEY = "pipeline:summaries"
SUMMARY_DLQ_KEY = "pipeline:summaries:dlq"
SUMMARY_GROUP_NAME = "summary-workers"


@dataclass
class DocumentSummaryTask:
    """One document-summary generation task."""

    source: str
    doc_id: str
    kb_id: str | None = None
    session_id: str | None = None
    tenant_id: str | None = None
    filename: str | None = None
    retry_count: int = 0
    created_at: float = 0.0
    trace_id: str = ""


class DocumentSummaryTaskCodec:
    """JSON codec for :class:`DocumentSummaryTask`."""

    def encode(self, task: DocumentSummaryTask) -> dict[str, str]:
        if task.created_at == 0.0:
            task.created_at = time.time()
        if not task.trace_id:
            task.trace_id = str(uuid.uuid4())
        return {"data": json.dumps(asdict(task), ensure_ascii=False)}

    def decode(
        self, msg_id: Any, fields: dict[Any, Any]
    ) -> tuple[str, DocumentSummaryTask] | None:
        try:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
            raw = fields.get(b"data") or fields.get("data")
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = json.loads(raw)
            task = DocumentSummaryTask(
                source=str(data["source"]),
                doc_id=str(data["doc_id"]),
                kb_id=data.get("kb_id"),
                session_id=data.get("session_id"),
                tenant_id=data.get("tenant_id"),
                filename=data.get("filename"),
                retry_count=int(data.get("retry_count", 0)),
                created_at=float(data.get("created_at", 0.0)),
                trace_id=str(data.get("trace_id", "")),
            )
            return mid, task
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("Failed to parse summary task: %s", exc)
            return None


class DocumentSummaryQueue(RedisStreamQueue[DocumentSummaryTask]):
    """Thin queue specialization for summary generation."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream_key: str = SUMMARY_STREAM_KEY,
        dlq_key: str = SUMMARY_DLQ_KEY,
        group_name: str = SUMMARY_GROUP_NAME,
        codec: DocumentSummaryTaskCodec | None = None,
    ) -> None:
        super().__init__(
            redis_client=redis_client,
            codec=codec or DocumentSummaryTaskCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )

    @classmethod
    async def create(
        cls,
        redis_url: str,
        stream_key: str = SUMMARY_STREAM_KEY,
        dlq_key: str = SUMMARY_DLQ_KEY,
        group_name: str = SUMMARY_GROUP_NAME,
    ) -> "DocumentSummaryQueue | None":
        queue = await super().create(
            redis_url=redis_url,
            codec=DocumentSummaryTaskCodec(),
            stream_key=stream_key,
            dlq_key=dlq_key,
            group_name=group_name,
        )
        return queue  # type: ignore[return-value]


_queue_instance: DocumentSummaryQueue | None = None


def get_document_summary_queue() -> DocumentSummaryQueue | None:
    """Return the process-local queue singleton, if initialized."""
    return _queue_instance


def set_document_summary_queue(queue: DocumentSummaryQueue | None) -> None:
    """Inject the process-local queue singleton."""
    global _queue_instance
    _queue_instance = queue


__all__ = [
    "DocumentSummaryTask",
    "DocumentSummaryQueue",
    "SUMMARY_STREAM_KEY",
    "SUMMARY_DLQ_KEY",
    "SUMMARY_GROUP_NAME",
    "get_document_summary_queue",
    "set_document_summary_queue",
]
