"""One-shot database migration entrypoint.

Production Compose runs this module once before backend and worker containers. It
serializes DDL with a PostgreSQL advisory lock and fails quickly when another
transaction holds an incompatible table lock.

Usage:
    python -m app.migrate
"""

from __future__ import annotations

import asyncio
import logging

from app.logging_config import setup_logging

setup_logging(service_name="migrate")

from app.config import get_settings
from app.storage.database import apply_migrations, engine

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    logger.info(
        "Starting database migration: lock_timeout=%.1fs statement_timeout=%.1fs",
        settings.migration_lock_timeout_seconds,
        settings.migration_statement_timeout_seconds,
    )
    try:
        await apply_migrations()
    finally:
        await engine.dispose()
    logger.info("Database migration completed")


if __name__ == "__main__":
    asyncio.run(main())
