"""Domain database connection management — pragmas, context manager, init.

The domain DB (`oracle_data.db`) is intentionally separate from the
LangGraph checkpoint DB (`oracle.db`) — see the `oracle.db` package
docstring for why.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from .schema import SCHEMA_SQL, SCHEMA_VERSION, _migrate_v4_to_v5

log = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

# Default location — can be overridden per-call (e.g. for tests).
# Step 4 will move this to Settings.data_db_path when config arrives.
DB_PATH = Path("oracle_data.db")

# Connection-level pragmas — applied on every aiosqlite.connect.
# - foreign_keys: enforce FK constraints (off by default in SQLite, surprising)
# - journal_mode WAL: Write-Ahead Logging — concurrent reads while a writer is active
# - synchronous NORMAL: balanced durability/speed (safe with WAL)
# - busy_timeout 5000ms: wait up to 5s for locks instead of failing immediately
PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
)


# ============================================================================
# Connection helpers
# ============================================================================


async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
    for pragma in PRAGMAS:
        await conn.execute(pragma)


@asynccontextmanager
async def get_db(db_path: Path | str = DB_PATH) -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager yielding a configured aiosqlite connection.

    Use from agent nodes to read/write domain tables. The connection has:
      - Row factory set to aiosqlite.Row (dict-like access by column name)
      - Foreign keys enforced
      - WAL journaling for concurrent reads
      - 5s busy timeout

    Call init_db() at app startup once before using this.
    """
    conn = await aiosqlite.connect(str(db_path))
    try:
        conn.row_factory = aiosqlite.Row
        await _apply_pragmas(conn)
        yield conn
    finally:
        await conn.close()


async def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create schema if missing, record schema version. Idempotent.

    Safe to call on every startup. Future migrations will go in here too —
    bump SCHEMA_VERSION and add migration blocks per version.
    """
    log.info("init_db: ensuring schema at %s (version %d)", db_path, SCHEMA_VERSION)
    async with get_db(db_path) as conn:
        await conn.executescript(SCHEMA_SQL)
        # Apply v4 -> v5 migration (idempotent — checks PRAGMA table_info)
        await _migrate_v4_to_v5(conn)
        await conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()
    log.info("init_db: schema ready")
