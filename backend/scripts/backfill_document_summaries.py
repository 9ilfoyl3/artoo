"""Schedule durable summaries for already completed documents/session files.

This is an operational backfill utility, not an API contract. It is safe to run
repeatedly: unchanged completed artifacts are skipped.

Usage:
    cd backend && ../.venv/bin/python -m scripts.backfill_document_summaries \
      --kb-id <kb_id>
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.schema.db import Document, SessionFile
from app.storage.database import async_session
from app.summary.service import (
    initialize_document_summary_queue,
    schedule_document_summary,
)


async def run(
    kb_id: str | None,
    include_session_files: bool,
    doc_id: str | None,
    force: bool,
) -> dict:
    queue = await initialize_document_summary_queue(get_settings().redis_url)
    if queue is None:
        raise RuntimeError("Redis summary queue is unavailable")

    scheduled = 0
    skipped = 0
    failed = 0
    async with async_session() as session:
        conditions = [Document.status == "completed"]
        if kb_id:
            conditions.append(Document.kb_id == kb_id)
        if doc_id:
            conditions.append(Document.id == doc_id)
        documents = (
            await session.execute(select(Document.id, Document.kb_id, Document.tenant_id).where(*conditions))
        ).all()

        session_files = []
        if include_session_files:
            session_files = (
                await session.execute(
                    select(
                        SessionFile.id,
                        SessionFile.session_id,
                        SessionFile.tenant_id,
                    ).where(SessionFile.status == "completed")
                )
            ).all()

    for doc_id, doc_kb_id, tenant_id in documents:
        try:
            changed = await schedule_document_summary(
                source="document",
                doc_id=doc_id,
                kb_id=doc_kb_id,
                tenant_id=tenant_id,
                force=force,
            )
            scheduled += int(changed)
            skipped += int(not changed)
        except Exception:
            failed += 1

    for file_id, session_id, tenant_id in session_files:
        try:
            changed = await schedule_document_summary(
                source="session-file",
                doc_id=file_id,
                session_id=session_id,
                tenant_id=tenant_id,
                force=force,
            )
            scheduled += int(changed)
            skipped += int(not changed)
        except Exception:
            failed += 1

    return {
        "documents_examined": len(documents),
        "session_files_examined": len(session_files),
        "scheduled": scheduled,
        "skipped": skipped,
        "failed": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id")
    parser.add_argument("--doc-id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-session-files", action="store_true")
    args = parser.parse_args()
    print(
        asyncio.run(
            run(args.kb_id, args.include_session_files, args.doc_id, args.force)
        )
    )


if __name__ == "__main__":
    main()
