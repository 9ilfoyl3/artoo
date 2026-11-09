"""Material token-estimation API tests.

The route is exercised directly with an in-memory SQLite session so the tests
cover the contract without booting the full application. A deterministic fake
estimator makes the expected character/token totals explicit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api import document as document_api
from app.api.errors import CrossTenantError
from app.auth.constants import TenantRoleEnum
from app.auth.identity import (
    IdentityContext,
    IdentitySourceEnum,
    OperationLevelEnum,
)
from app.schema.db import Base, Chunk, Document, KnowledgeBase


class _CharEstimator:
    """Deterministic estimator: one token per Unicode character."""

    is_fallback = False

    def estimate_string(self, text: str) -> int:
        return len(text)

    def estimate_string_with_fallback(self, text: str) -> tuple[int, bool]:
        return len(text), False


class _FallbackEstimator(_CharEstimator):
    """Deterministic estimator that reports a per-document fallback."""

    def estimate_string_with_fallback(self, text: str) -> tuple[int, bool]:
        return len(text), True


def _identity(user_id: str = "owner-1") -> IdentityContext:
    return IdentityContext(
        source=IdentitySourceEnum.JWT,
        op_level=OperationLevelEnum.TENANT,
        tenant_id="tenant-1",
        user_id=user_id,
        role=TenantRoleEnum.MEMBER,
    )


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        session.add(
            KnowledgeBase(
                id="kb-1",
                name="KB",
                owner_user_id="owner-1",
                tenant_id="tenant-1",
            )
        )
        session.add_all(
            [
                Document(
                    id="doc-1",
                    kb_id="kb-1",
                    filename="one.md",
                    file_type="md",
                    status="completed",
                    tenant_id="tenant-1",
                ),
                Document(
                    id="doc-2",
                    kb_id="kb-1",
                    filename="two.txt",
                    file_type="txt",
                    status="completed",
                    tenant_id="tenant-1",
                ),
                Document(
                    id="doc-3",
                    kb_id="kb-1",
                    filename="pending.pdf",
                    file_type="pdf",
                    status="pending",
                    tenant_id="tenant-1",
                ),
            ]
        )
        session.add_all(
            [
                Chunk(
                    id="chunk-1a",
                    doc_id="doc-1",
                    kb_id="kb-1",
                    parent_id=None,
                    content="abc",
                    chunk_index=0,
                    tenant_id="tenant-1",
                ),
                Chunk(
                    id="chunk-1b",
                    doc_id="doc-1",
                    kb_id="kb-1",
                    parent_id=None,
                    content="defgh",
                    chunk_index=1,
                    tenant_id="tenant-1",
                ),
                Chunk(
                    id="chunk-1-child",
                    doc_id="doc-1",
                    kb_id="kb-1",
                    parent_id="chunk-1a",
                    content="child text must not be counted",
                    chunk_index=0,
                    tenant_id="tenant-1",
                ),
                Chunk(
                    id="chunk-2a",
                    doc_id="doc-2",
                    kb_id="kb-1",
                    parent_id=None,
                    content="xy",
                    chunk_index=0,
                    tenant_id="tenant-1",
                ),
            ]
        )
        await session.commit()
    return engine, session_factory


@pytest.mark.asyncio
async def test_estimate_all_documents_in_knowledge_base(monkeypatch):
    engine, session_factory = await _make_session()
    monkeypatch.setattr(
        document_api, "_get_material_token_estimator", lambda: _CharEstimator()
    )
    try:
        async with session_factory() as session:
            response = await document_api.estimate_material_tokens(
                request=document_api.MaterialTokenEstimateRequest(
                    knowledge_base_id="kb-1"
                ),
                identity=_identity(),
                db=session,
            )

        result = response.result
        assert result.scope == "knowledge_base"
        assert result.knowledge_base_id == "kb-1"
        assert result.document_count == 3
        assert result.total_characters == 12
        assert result.total_tokens == 12
        assert result.encoding == "cl100k_base"
        assert result.fallback is False

        by_id = {item.file_id: item for item in result.files}
        assert by_id["doc-1"].character_count == 10
        assert by_id["doc-1"].token_count == 10
        assert by_id["doc-2"].character_count == 2
        assert by_id["doc-3"].character_count == 0
        assert by_id["doc-3"].token_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_estimate_selected_file_ids_preserves_order_and_deduplicates(
    monkeypatch,
):
    engine, session_factory = await _make_session()
    monkeypatch.setattr(
        document_api, "_get_material_token_estimator", lambda: _CharEstimator()
    )
    try:
        async with session_factory() as session:
            response = await document_api.estimate_material_tokens(
                request=document_api.MaterialTokenEstimateRequest(
                    file_ids=["doc-2", "doc-1", "doc-2"]
                ),
                identity=_identity(),
                db=session,
            )

        result = response.result
        assert result.scope == "files"
        assert result.knowledge_base_id is None
        assert result.document_count == 2
        assert result.total_characters == 12
        assert result.total_tokens == 12
        assert [item.file_id for item in result.files] == ["doc-2", "doc-1"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_estimate_reports_per_document_fallback(monkeypatch):
    engine, session_factory = await _make_session()
    monkeypatch.setattr(
        document_api,
        "_get_material_token_estimator",
        lambda: _FallbackEstimator(),
    )
    try:
        async with session_factory() as session:
            response = await document_api.estimate_material_tokens(
                request=document_api.MaterialTokenEstimateRequest(
                    file_ids=["doc-1"]
                ),
                identity=_identity(),
                db=session,
            )

        assert response.result.fallback is True
    finally:
        await engine.dispose()


def test_request_requires_exactly_one_scope():
    with pytest.raises(ValidationError):
        document_api.MaterialTokenEstimateRequest()

    with pytest.raises(ValidationError):
        document_api.MaterialTokenEstimateRequest(
            knowledge_base_id="kb-1",
            file_ids=["doc-1"],
        )


@pytest.mark.asyncio
async def test_unknown_file_id_returns_not_found_semantics():
    engine, session_factory = await _make_session()
    try:
        async with session_factory() as session:
            with pytest.raises(CrossTenantError):
                await document_api.estimate_material_tokens(
                    request=document_api.MaterialTokenEstimateRequest(
                        file_ids=["missing-doc"]
                    ),
                    identity=_identity(),
                    db=session,
                )
    finally:
        await engine.dispose()
