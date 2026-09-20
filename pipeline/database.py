"""
Database connection manager.

Provides a shared asyncpg connection pool with optimized settings
for low-latency hybrid search queries.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg

from pipeline import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_search_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection):
    """Per-connection initialization — runs once when a connection is created."""
    # Pre-set HNSW search accuracy for all vector queries on this connection.
    await conn.execute(f"SET hnsw.ef_search = {max(int(settings.hnsw_ef_search), 60)}")
    # Enable JIT for complex query plans over filter CTEs.
    await conn.execute("SET jit = on")


async def get_pool() -> asyncpg.Pool:
    """Get or create the shared connection pool."""
    global _pool
    if _pool is None:
        _pool = await create_pool(label="primary")
    return _pool


async def get_search_pool() -> asyncpg.Pool:
    """Get the read/search pool, falling back to the primary pool.

    In a Supabase + ParadeDB deployment, writes/ingest stay on DATABASE_URL and
    search reads use SEARCH_DATABASE_URL. Local/dev deployments usually leave
    SEARCH_DATABASE_URL empty, so this returns the same primary pool.
    """
    global _search_pool
    dsn = (settings.search_database_url or "").strip()
    if not dsn:
        return await get_pool()
    if _search_pool is None:
        min_size = settings.search_db_pool_min or settings.db_pool_min
        max_size = settings.search_db_pool_max or settings.db_pool_max
        _search_pool = await create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            label="search",
        )
    return _search_pool


async def create_pool(
    dsn: str | None = None,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    label: str = "primary",
) -> asyncpg.Pool:
    """
    Create an asyncpg connection pool with performance-tuned settings.

    Each connection is initialized with:
      - statement_cache_size=100: caches prepared statements for repeated queries
      - hnsw.ef_search: pre-set for vector search accuracy/speed tradeoff
    """
    min_size = settings.db_pool_min if min_size is None else int(min_size)
    max_size = settings.db_pool_max if max_size is None else int(max_size)

    pool = await asyncpg.create_pool(
        dsn=dsn or settings.database_url,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=100,       # cache prepared statements
        max_inactive_connection_lifetime=300,
        init=_init_connection,
    )

    logger.info(
        "%s connection pool created (min=%d, max=%d, ef_search=%d)",
        label, min_size, max_size, max(int(settings.hnsw_ef_search), 60),
    )
    return pool


async def close_pool():
    """Gracefully close connection pools."""
    global _pool, _search_pool
    if _search_pool:
        await _search_pool.close()
        _search_pool = None
        logger.info("Search connection pool closed")
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Primary connection pool closed")


@asynccontextmanager
async def get_connection():
    """Context manager for a single connection from the pool."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def get_search_connection():
    """Context manager for a single search/read connection."""
    pool = await get_search_pool()
    async with pool.acquire() as conn:
        yield conn
