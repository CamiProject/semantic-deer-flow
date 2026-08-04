"""Deterministic local hashing embeddings shared by small FAISS indexes."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Protocol


class EmbeddingProvider(Protocol):
    dimension: int

    def encode(self, texts: Sequence[str]) -> Any:
        """Return a float32 matrix with one normalized vector per text."""


class HashingEmbeddingProvider:
    """Generate normalized character n-gram vectors without network access."""

    def __init__(self, *, dimension: int, model_name: str) -> None:
        self.dimension = dimension
        self.model_name = model_name

    def encode(self, texts: Sequence[str]) -> Any:
        import numpy as np

        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            normalized = " ".join(str(text).lower().split())
            grams = [normalized[index : index + size] for size in (1, 2, 3) for index in range(max(0, len(normalized) - size + 1))]
            for gram in grams:
                digest = hashlib.blake2b(f"{self.model_name}:{gram}".encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                matrix[row, bucket] += sign
            norm = float(np.linalg.norm(matrix[row]))
            if norm:
                matrix[row] /= norm
        return matrix
