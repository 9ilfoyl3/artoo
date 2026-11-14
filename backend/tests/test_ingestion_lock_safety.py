"""Regression coverage for ingestion/migration lock isolation."""

from __future__ import annotations

import asyncio
import contextlib
import os

os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret-please-use-32bytes-min")

from app.pipeline.queue import TaskMessage
from app.pipeline.worker import PipelineWorker
from app.storage.database import (
    _idempotent_migration_sql,
    _migration_statements,
    _timeout_literal,
)


class _Queue:
    def __init__(self) -> None:
        self.acked: list[str] = []

    async def ack(self, message_id: str) -> None:
        self.acked.append(message_id)


class _Pipeline:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    async def process(self, **_kwargs) -> None:
        self.timeline.append("pipeline")


@contextlib.asynccontextmanager
async def _resolved_file(_msg: TaskMessage):
    yield "/tmp/test.pdf"


def _message() -> TaskMessage:
    return TaskMessage(
        doc_id="doc-1",
        kb_id="kb-1",
        file_path="/tmp/test.pdf",
        tenant_id="tenant-1",
        object_key="kb-1/test.pdf",
    )


def test_worker_checks_document_status_only_after_processing_slot() -> None:
    async def run() -> list[str]:
        timeline: list[str] = []
        queue = _Queue()
        worker = PipelineWorker(
            queue=queue,
            pipeline=_Pipeline(timeline),
            db_session_factory=object(),
            max_concurrent=1,
        )

        async def _is_completed(_doc_id: str) -> bool:
            timeline.append("status")
            return False

        worker._is_document_completed = _is_completed
        worker._resolve_file = _resolved_file  # type: ignore[method-assign]

        await worker.semaphore.acquire()
        task = asyncio.create_task(worker._process_task("1-0", _message(), queue=queue))
        await asyncio.sleep(0.05)
        assert timeline == []
        assert not task.done()

        worker.semaphore.release()
        await task
        assert timeline == ["status", "pipeline"]
        assert queue.acked == ["1-0"]
        return timeline

    asyncio.run(run())


def test_worker_dispatch_waits_when_all_slots_have_tasks() -> None:
    async def run() -> None:
        worker = PipelineWorker(
            queue=_Queue(),
            pipeline=_Pipeline([]),
            db_session_factory=object(),
            max_concurrent=1,
        )
        worker._running = True
        blocker = asyncio.create_task(asyncio.sleep(10))
        worker._tasks.add(blocker)
        try:
            try:
                await asyncio.wait_for(worker._wait_for_task_capacity(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("dispatch should wait for a free processing slot")
        finally:
            worker._running = False
            blocker.cancel()
            await asyncio.gather(blocker, return_exceptions=True)

    asyncio.run(run())


def test_add_column_migrations_are_replay_safe() -> None:
    statements = _migration_statements()

    assert any("session_files ADD COLUMN progress" in sql for sql in statements)
    assert any("chat_messages ADD COLUMN agent_steps" in sql for sql in statements)
    for sql in statements:
        if " ADD COLUMN " in sql.upper() and " IF NOT EXISTS " not in sql.upper():
            assert "IF NOT EXISTS" in _idempotent_migration_sql(sql).upper()
    assert _idempotent_migration_sql(
        "ALTER TABLE documents ADD COLUMN folder_id VARCHAR"
    ) == "ALTER TABLE documents ADD COLUMN IF NOT EXISTS folder_id VARCHAR"
    assert "IF NOT EXISTS" in _idempotent_migration_sql(
        "ALTER TABLE documents ADD COLUMN IF NOT EXISTS folder_id VARCHAR"
    )


def test_migration_timeout_literal_is_bounded_milliseconds() -> None:
    assert _timeout_literal(0) == "1ms"
    assert _timeout_literal(5) == "5000ms"
    assert _timeout_literal(0.25) == "250ms"
