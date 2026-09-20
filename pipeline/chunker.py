"""
Token-aware text chunking with configurable overlap.

Ensures each chunk fits within the embedding model's token budget
and maintains semantic continuity via sliding-window overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

from pipeline import settings


@dataclass
class Chunk:
    """A single text chunk with metadata."""
    index: int
    content: str
    token_count: int


class TextChunker:
    """
    Token-aware text chunker.

    Uses tiktoken to count tokens accurately for the configured embedding model.
    Supports two strategies:
      - "sliding_window": fixed token-size windows with overlap (default)
      - "paragraph":      split on paragraph boundaries, merge small ones
    """

    def __init__(
        self,
        chunk_size: int = settings.chunk_size,
        chunk_overlap: int = settings.chunk_overlap,
        model: str = settings.embedding_model,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        try:
            self.encoder = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback for models tiktoken doesn't know about (e.g., Gemini)
            self.encoder = tiktoken.get_encoding("cl100k_base")

    # ─── Public API ───────────────────────────

    def chunk(self, text: str, strategy: str = "sliding_window") -> list[Chunk]:
        """
        Split text into token-bounded chunks.

        Args:
            text: The raw text to chunk.
            strategy: "sliding_window" or "paragraph".

        Returns:
            List of Chunk objects with index, content, and token count.
        """
        text = self._clean(text)
        if not text.strip():
            return []

        if strategy == "paragraph":
            return self._chunk_by_paragraph(text)
        return self._chunk_sliding_window(text)

    # ─── Strategies ───────────────────────────

    def _chunk_sliding_window(self, text: str) -> list[Chunk]:
        """Fixed-size sliding window over tokens with overlap."""
        tokens = self.encoder.encode(text)
        chunks: list[Chunk] = []
        start = 0
        idx = 0

        while start < len(tokens):
            end = min(start + self.chunk_size, len(tokens))
            chunk_tokens = tokens[start:end]
            content = self.encoder.decode(chunk_tokens)

            chunks.append(Chunk(
                index=idx,
                content=content.strip(),
                token_count=len(chunk_tokens),
            ))

            # Slide the window forward
            start += self.chunk_size - self.chunk_overlap
            idx += 1

        return chunks

    def _chunk_by_paragraph(self, text: str) -> list[Chunk]:
        """
        Split on paragraph boundaries, then merge small paragraphs
        to approach the target chunk size.
        """
        paragraphs = re.split(r"\n\s*\n", text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks: list[Chunk] = []
        current_parts: list[str] = []
        current_tokens = 0
        idx = 0

        for para in paragraphs:
            para_tokens = len(self.encoder.encode(para))

            # If a single paragraph exceeds chunk_size, sub-chunk it
            if para_tokens > self.chunk_size:
                # Flush current buffer first
                if current_parts:
                    content = "\n\n".join(current_parts)
                    chunks.append(Chunk(index=idx, content=content, token_count=current_tokens))
                    idx += 1
                    current_parts = []
                    current_tokens = 0

                # Sub-chunk the large paragraph with sliding window
                sub_chunks = self._chunk_sliding_window(para)
                for sc in sub_chunks:
                    sc.index = idx
                    chunks.append(sc)
                    idx += 1
                continue

            # Would adding this paragraph exceed the limit?
            if current_tokens + para_tokens > self.chunk_size and current_parts:
                content = "\n\n".join(current_parts)
                chunks.append(Chunk(index=idx, content=content, token_count=current_tokens))
                idx += 1
                current_parts = []
                current_tokens = 0

            current_parts.append(para)
            current_tokens += para_tokens

        # Flush remaining
        if current_parts:
            content = "\n\n".join(current_parts)
            chunks.append(Chunk(index=idx, content=content, token_count=current_tokens))

        return chunks

    # ─── Helpers ──────────────────────────────

    @staticmethod
    def _clean(text: str) -> str:
        """Normalize whitespace and remove control characters."""
        # Replace tabs with spaces
        text = text.replace("\t", " ")
        # Collapse multiple spaces (but preserve newlines for paragraph detection)
        text = re.sub(r"[^\S\n]+", " ", text)
        # Remove control characters except newline
        text = re.sub(r"[\x00-\x09\x0b-\x0c\x0e-\x1f\x7f]", "", text)
        return text.strip()
