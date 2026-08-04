"""FAISS second-stage adapter with a lightweight local embedding provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.gateway.model_routing.contracts import RouteType, RoutingSignals
from deerflow.config.model_routing_config import FaissRoutingConfig
from deerflow.config.runtime_paths import resolve_path
from deerflow.utils.hashing_embedding import EmbeddingProvider, HashingEmbeddingProvider


class FaissSearchError(RuntimeError):
    """Raised when the local FAISS index cannot be used."""


class FaissConfigurationError(FaissSearchError):
    """Raised when FAISS assets do not match the configured route contract."""


class FaissSearchResult:
    __slots__ = ("route_type", "confidence", "reason_codes", "signals", "index_version")

    def __init__(
        self,
        route_type: RouteType,
        *,
        confidence: float,
        reason_codes: tuple[str, ...],
        signals: RoutingSignals,
        index_version: str,
    ) -> None:
        self.route_type = route_type
        self.confidence = confidence
        self.reason_codes = reason_codes
        self.signals = signals
        self.index_version = index_version


class FaissSearcher:
    """Process-local, read-only FAISS index and sample lookup."""

    def __init__(self, config: FaissRoutingConfig) -> None:
        self.config = config
        self._load_lock = threading.Lock()
        self._index: Any | None = None
        self._examples: list[dict[str, Any]] = []
        self._provider: EmbeddingProvider | None = None

    async def search(self, question: str) -> FaissSearchResult | None:
        return await asyncio.to_thread(self._search_sync, question)

    def _search_sync(self, question: str) -> FaissSearchResult | None:
        index, examples, provider = self._ensure_loaded()
        try:
            vectors = provider.encode([question])
            distances, indices = index.search(vectors, self.config.top_k)
        except FaissSearchError:
            raise
        except Exception as exc:
            raise FaissSearchError("FAISS similarity search failed") from exc

        votes: dict[RouteType, float] = {"simple": 0.0, "complex": 0.0}
        counts: dict[RouteType, int] = {"simple": 0, "complex": 0}
        candidate_count = 0
        candidate_signals: dict[RouteType, RoutingSignals] = {}
        for score, raw_index in zip(distances[0], indices[0], strict=False):
            sample_index = int(raw_index)
            if sample_index < 0 or float(score) < self.config.similarity_threshold:
                continue
            sample = examples[sample_index]
            label = sample.get("route_type")
            if label not in ("simple", "complex"):
                continue
            route_type: RouteType = label
            weight = max(0.0, float(score))
            votes[route_type] += weight
            counts[route_type] += 1
            candidate_count += 1
            candidate_signals.setdefault(route_type, _signals_from_sample(sample))

        if candidate_count == 0:
            return None

        selected = "simple" if votes["simple"] > votes["complex"] else "complex" if votes["complex"] > votes["simple"] else None
        if selected is None or counts[selected] < self.config.min_votes:
            return None
        total_weight = votes["simple"] + votes["complex"]
        confidence = votes[selected] / total_weight if total_weight else 0.0
        if confidence < 0.6:
            return None
        return FaissSearchResult(
            selected,
            confidence=confidence,
            reason_codes=("faiss_similarity_vote",),
            signals=candidate_signals.get(selected, RoutingSignals()),
            index_version=self.config.index_version,
        )

    def _ensure_loaded(self) -> tuple[Any, list[dict[str, Any]], EmbeddingProvider]:
        if self._index is not None and self._provider is not None:
            return self._index, self._examples, self._provider
        with self._load_lock:
            if self._index is not None and self._provider is not None:
                return self._index, self._examples, self._provider
            try:
                import faiss
            except ImportError as exc:
                raise FaissConfigurationError("FAISS is not installed; install the model-routing extra") from exc

            index_path = resolve_path(self.config.index_path)
            examples_path = resolve_path(self.config.examples_path)
            metadata_path = resolve_path(self.config.metadata_path)
            if not index_path.is_file():
                raise FaissConfigurationError(f"FAISS index not found: {index_path}")
            if not examples_path.is_file():
                raise FaissConfigurationError(f"FAISS examples not found: {examples_path}")
            if not metadata_path.is_file():
                raise FaissConfigurationError(f"FAISS metadata not found: {metadata_path}")
            try:
                index = faiss.read_index(str(index_path))
            except Exception as exc:
                raise FaissConfigurationError(f"Failed to load FAISS index: {exc}") from exc
            examples = _load_examples(examples_path, expected_label_version=self.config.label_version)
            metadata = _load_metadata(metadata_path)
            try:
                _validate_asset_metadata(
                    metadata,
                    config=self.config,
                    example_count=len(examples),
                    examples_path=examples_path,
                )
            except Exception as exc:
                if isinstance(exc, FaissConfigurationError):
                    raise
                raise FaissConfigurationError(f"Failed to validate FAISS routing assets: {exc}") from exc
            if int(index.ntotal) != len(examples):
                raise FaissConfigurationError("FAISS index size does not match routing examples")

            provider = _create_embedding_provider(self.config)
            if int(index.d) != provider.dimension:
                raise FaissConfigurationError(f"FAISS dimension {index.d} does not match embedding dimension {provider.dimension}")
            self._index = index
            self._examples = examples
            self._provider = provider
            return index, examples, provider


_SEARCHER_CACHE_KEY: tuple[object, ...] | None = None
_SEARCHER_CACHE_VALUE: FaissSearcher | None = None
_SEARCHER_CACHE_LOCK = threading.Lock()


def get_faiss_searcher(config: FaissRoutingConfig) -> FaissSearcher:
    """Return one read-only searcher per process and configuration version."""
    global _SEARCHER_CACHE_KEY, _SEARCHER_CACHE_VALUE
    key = (
        config.index_path,
        config.examples_path,
        config.embedding_provider,
        config.embedding_model,
        config.embedding_dimension,
        config.top_k,
        config.similarity_threshold,
        config.min_votes,
        config.label_version,
        config.index_version,
        config.metadata_path,
    )
    with _SEARCHER_CACHE_LOCK:
        if _SEARCHER_CACHE_KEY != key or _SEARCHER_CACHE_VALUE is None:
            _SEARCHER_CACHE_KEY = key
            _SEARCHER_CACHE_VALUE = FaissSearcher(config)
        return _SEARCHER_CACHE_VALUE


def build_faiss_index(config: FaissRoutingConfig) -> None:
    """Build an inner-product index from the configured JSONL examples."""
    try:
        import faiss
    except ImportError as exc:
        raise FaissConfigurationError("FAISS is not installed; install the model-routing extra") from exc

    examples_path = resolve_path(config.examples_path)
    index_path = resolve_path(config.index_path)
    metadata_path = resolve_path(config.metadata_path)
    examples = _load_examples(examples_path, expected_label_version=config.label_version)
    provider = _create_embedding_provider(config)
    vectors = provider.encode([str(sample["text"]) for sample in examples])
    index = faiss.IndexFlatIP(provider.dimension)
    index.add(vectors)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_path))
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": "1",
        "index_version": config.index_version,
        "label_version": config.label_version,
        "embedding_provider": config.embedding_provider,
        "embedding_model": config.embedding_model,
        "embedding_dimension": config.embedding_dimension,
        "example_count": len(examples),
        "examples_sha256": _examples_sha256(examples_path),
    }
    temporary_path = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(metadata_path)


def _create_embedding_provider(config: FaissRoutingConfig) -> EmbeddingProvider:
    if config.embedding_provider == "hashing":
        return HashingEmbeddingProvider(dimension=config.embedding_dimension, model_name=config.embedding_model)
    raise FaissConfigurationError(f"Unsupported embedding provider: {config.embedding_provider}")


def _load_examples(path: Path, *, expected_label_version: str) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FaissConfigurationError(f"Invalid routing example JSON at line {line_number}") from exc
        if not isinstance(sample, dict) or not isinstance(sample.get("text"), str) or sample.get("route_type") not in {"simple", "complex"}:
            raise FaissConfigurationError(f"Invalid routing example at line {line_number}")
        if sample.get("label_version") != expected_label_version:
            raise FaissConfigurationError(f"Routing example label version mismatch at line {line_number}")
        examples.append(sample)
    if not examples:
        raise FaissConfigurationError("FAISS routing examples are empty")
    return examples


def _load_metadata(path: Path) -> Mapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FaissConfigurationError(f"Invalid FAISS metadata: {path}") from exc
    if not isinstance(raw, Mapping):
        raise FaissConfigurationError("FAISS metadata must be a JSON object")
    return raw


def _validate_asset_metadata(
    metadata: Mapping[str, Any],
    *,
    config: FaissRoutingConfig,
    example_count: int,
    examples_path: Path,
) -> None:
    expected = {
        "schema_version": "1",
        "index_version": config.index_version,
        "label_version": config.label_version,
        "embedding_provider": config.embedding_provider,
        "embedding_model": config.embedding_model,
        "embedding_dimension": config.embedding_dimension,
        "example_count": example_count,
        "examples_sha256": _examples_sha256(examples_path),
    }
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise FaissConfigurationError(f"FAISS metadata mismatch for {key}")


def _examples_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signals_from_sample(sample: dict[str, Any]) -> RoutingSignals:
    raw = sample.get("signals")
    if not isinstance(raw, dict):
        return RoutingSignals()
    return RoutingSignals(
        risk_level=str(raw.get("risk_level", "unknown")),
        difficulty_level=str(raw.get("difficulty_level", "unknown")),
        scale_level=str(raw.get("scale_level", "unknown")),
        delivery_level=str(raw.get("delivery_level", "unknown")),
    )
