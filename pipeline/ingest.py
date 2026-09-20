"""
Document ingestion pipeline.

Handles the full flow: raw text → chunk → embed → store in PostgreSQL.
Supports deduplication via content hashing and idempotent re-ingestion.
"""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4
from typing import Optional

import asyncpg

from pipeline import cache as _cache
from pipeline import settings
from pipeline.chunker import TextChunker
from pipeline.embedder import EmbeddingProvider, get_embedder

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """
    Ingests documents for a candidate:
    1. Hash-check for deduplication
    2. Store raw document
    3. Chunk the text
    4. Batch-embed all chunks
    5. Insert chunks + vectors into PostgreSQL
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: Optional[EmbeddingProvider] = None,
        chunker: Optional[TextChunker] = None,
    ):
        self.pool = pool
        self.embedder = embedder or get_embedder()
        self.chunker = chunker or TextChunker()

    async def ingest(
        self,
        candidate_id: str,
        doc_type: str,
        title: str,
        raw_text: str,
        metadata: Optional[dict] = None,
        chunk_strategy: str = "sliding_window",
    ) -> dict:
        """
        Full ingestion pipeline for a single document.

        Args:
            candidate_id: UUID of the candidate this document belongs to.
            doc_type: One of: transcript, certification, resume, bio, cover_letter, other.
            title: Human-readable document title.
            raw_text: The full document text.
            metadata: Optional JSON metadata to store with each chunk.
            chunk_strategy: "sliding_window" or "paragraph".

        Returns:
            dict with document_id, chunk_count, and status.
        """
        metadata = metadata or {}

        # ─── Step 1: Deduplication check ──────
        file_hash = hashlib.sha256(raw_text.encode()).hexdigest()
        existing = await self.pool.fetchval(
            "SELECT id FROM candidate_documents WHERE candidate_id = $1 AND file_hash = $2",
            candidate_id, file_hash,
        )
        if existing:
            logger.info("Document already ingested (hash=%s), skipping.", file_hash[:12])
            return {
                "document_id": str(existing),
                "chunk_count": 0,
                "status": "duplicate_skipped",
            }

        document_id = str(uuid4())

        # ─── Step 2: Store raw document ───────
        await self.pool.execute(
            """
            INSERT INTO candidate_documents (id, candidate_id, doc_type, title, raw_text, file_hash)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            document_id, candidate_id, doc_type, title, raw_text, file_hash,
        )

        # ─── Step 3: Chunk ────────────────────
        chunks = self.chunker.chunk(raw_text, strategy=chunk_strategy)
        if not chunks:
            logger.warning("No chunks produced from document %s", document_id)
            return {
                "document_id": document_id,
                "chunk_count": 0,
                "status": "empty_document",
            }

        logger.info("Chunked document %s into %d chunks", document_id, len(chunks))

        # ─── Step 4: Batch embed ──────────────
        chunk_texts = [c.content for c in chunks]
        all_embeddings: list[list[float]] = []

        for i in range(0, len(chunk_texts), settings.max_batch_size):
            batch = chunk_texts[i : i + settings.max_batch_size]
            embeddings = await self.embedder.embed(batch)
            all_embeddings.extend(embeddings)
            logger.debug(
                "Embedded batch %d/%d (%d texts)",
                i // settings.max_batch_size + 1,
                (len(chunk_texts) - 1) // settings.max_batch_size + 1,
                len(batch),
            )

        # ─── Step 5: Insert chunks + vectors ──
        records = []
        for chunk, embedding in zip(chunks, all_embeddings):
            chunk_meta = {**metadata, "chunk_index": chunk.index}
            records.append((
                str(uuid4()),
                candidate_id,
                document_id,
                chunk.index,
                chunk.content,
                str(embedding),      # pgvector accepts string representation
                chunk.token_count,
                json.dumps(chunk_meta),  # asyncpg needs JSONB as string
            ))

        await self.pool.executemany(
            """
            INSERT INTO document_chunks
                (id, candidate_id, document_id, chunk_index, content, embedding, token_count, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8::jsonb)
            """,
            records,
        )

        logger.info(
            "Ingested document %s: %d chunks, %d dimensions",
            document_id, len(chunks), self.embedder.dimensions,
        )

        return {
            "document_id": document_id,
            "chunk_count": len(chunks),
            "status": "ingested",
        }

    async def delete_document(self, document_id: str) -> int:
        """
        Delete a document and all its chunks.
        CASCADE handles chunk deletion automatically.

        Returns: number of chunks deleted.
        """
        rows = await self.pool.fetch(
            "SELECT id::text AS chunk_id FROM document_chunks WHERE document_id = $1",
            document_id,
        )
        chunk_ids = [r["chunk_id"] for r in rows]
        await self.pool.execute(
            "DELETE FROM candidate_documents WHERE id = $1", document_id,
        )
        await _cache.invalidate_many([_cache.chunk_key(chunk_id) for chunk_id in chunk_ids])
        logger.info("Deleted document %s (%d chunks)", document_id, len(chunk_ids))
        return len(chunk_ids)

    async def reingest_document(self, document_id: str) -> dict:
        """
        Re-embed an existing document (e.g., after model upgrade).
        Deletes old chunks and re-creates them from the stored raw_text.
        """
        row = await self.pool.fetchrow(
            "SELECT candidate_id, doc_type, title, raw_text FROM candidate_documents WHERE id = $1",
            document_id,
        )
        if not row:
            raise ValueError(f"Document {document_id} not found")

        # Delete old chunks (but keep the document record)
        chunk_rows = await self.pool.fetch(
            "SELECT id::text AS chunk_id FROM document_chunks WHERE document_id = $1",
            document_id,
        )
        await self.pool.execute(
            "DELETE FROM document_chunks WHERE document_id = $1", document_id,
        )
        await _cache.invalidate_many([
            _cache.chunk_key(r["chunk_id"]) for r in chunk_rows
        ])

        # Delete the document record too (ingest will recreate it)
        await self.pool.execute(
            "DELETE FROM candidate_documents WHERE id = $1", document_id,
        )

        return await self.ingest(
            candidate_id=str(row["candidate_id"]),
            doc_type=row["doc_type"],
            title=row["title"],
            raw_text=row["raw_text"],
        )
