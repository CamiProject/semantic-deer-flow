"""Local FAISS recall of Ontology object, metric, and Action candidates."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Literal

from deerflow.config.semantic_recall_config import SemanticRecallConfig
from deerflow.semantic.ontology import OntologyRegistry
from deerflow.utils.hashing_embedding import EmbeddingProvider, HashingEmbeddingProvider

logger = logging.getLogger(__name__)

OntologyKind = Literal["objects", "metrics", "actions"]
_KINDS: tuple[OntologyKind, ...] = ("objects", "metrics", "actions")
_ACTION_INTENT_RE = re.compile(
    r"(?:修改|更新|变更|调整|设置|替换|改名|重命名|换成|删除|新增|创建|启用|停用|"
    r"\b(?:change|changing|changed|rename|renaming|renamed|update|updating|updated|"
    r"modify|modifying|modified|set|setting|replace|replacing|edit|editing|write|"
    r"delete|create|enable|disable)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OntologyRecallResult:
    candidate_ids: dict[OntologyKind, tuple[str, ...]]
    source: Literal["disabled", "faiss", "none", "unavailable"]
    index_version: str
    max_score: float | None = None

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "index_version": self.index_version,
            "candidate_counts": {kind: len(self.candidate_ids[kind]) for kind in _KINDS},
            "max_score": self.max_score,
        }


class OntologyFaissRecaller:
    """Build and search a process-local index derived from reviewed Ontology aliases."""

    def __init__(self, ontology: OntologyRegistry, config: SemanticRecallConfig) -> None:
        self.ontology = ontology
        self.config = config
        self._load_lock = threading.Lock()
        self._index: Any | None = None
        self._samples: list[tuple[OntologyKind, str, str]] = []
        self._provider: EmbeddingProvider | None = None
        self._unavailable_reason: str | None = None

    def recall(self, question: str) -> OntologyRecallResult:
        empty = _empty_candidates()
        if not self.config.enabled:
            return OntologyRecallResult(empty, "disabled", self.ontology.version)
        if self._unavailable_reason is not None:
            return OntologyRecallResult(empty, "unavailable", self.ontology.version)
        try:
            index, samples, provider = self._ensure_index()
            vectors = provider.encode([question])
            search_count = min(self.config.top_k, len(samples))
            distances, indices = index.search(vectors, search_count)
        except Exception as exc:
            self._unavailable_reason = str(exc)[:500]
            logger.warning("Ontology FAISS recall unavailable; using exact matching: %s", exc)
            return OntologyRecallResult(empty, "unavailable", self.ontology.version)

        votes: dict[tuple[OntologyKind, str], list[float]] = {}
        action_intent = _ACTION_INTENT_RE.search(question) is not None
        for score, raw_index in zip(distances[0], indices[0], strict=False):
            sample_index = int(raw_index)
            normalized_score = float(score)
            if sample_index < 0 or normalized_score < self.config.similarity_threshold:
                continue
            kind, candidate_id, _text = samples[sample_index]
            if kind == "actions" and not action_intent:
                continue
            votes.setdefault((kind, candidate_id), []).append(normalized_score)

        selected: dict[OntologyKind, list[tuple[str, float]]] = {kind: [] for kind in _KINDS}
        for (kind, candidate_id), scores in votes.items():
            if len(scores) >= self.config.min_votes:
                selected[kind].append((candidate_id, max(scores)))

        candidates: dict[OntologyKind, tuple[str, ...]] = {}
        all_scores: list[float] = []
        for kind in _KINDS:
            ranked = sorted(selected[kind], key=lambda item: (-item[1], item[0]))[: self.config.max_candidates_per_kind]
            candidates[kind] = tuple(candidate_id for candidate_id, _score in ranked)
            all_scores.extend(score for _candidate_id, score in ranked)
        source = "faiss" if any(candidates.values()) else "none"
        return OntologyRecallResult(
            candidates,
            source,
            self.ontology.version,
            max(all_scores) if all_scores else None,
        )

    def _ensure_index(self) -> tuple[Any, list[tuple[OntologyKind, str, str]], EmbeddingProvider]:
        if self._index is not None and self._provider is not None:
            return self._index, self._samples, self._provider
        with self._load_lock:
            if self._index is not None and self._provider is not None:
                return self._index, self._samples, self._provider
            import faiss

            samples = _ontology_samples(self.ontology)
            if not samples:
                raise RuntimeError("Ontology contains no recallable aliases")
            provider = HashingEmbeddingProvider(
                dimension=self.config.embedding_dimension,
                model_name=self.config.embedding_model,
            )
            vectors = provider.encode([text for _kind, _candidate_id, text in samples])
            index = faiss.IndexFlatIP(provider.dimension)
            index.add(vectors)
            self._index = index
            self._samples = samples
            self._provider = provider
            return index, samples, provider


def _empty_candidates() -> dict[OntologyKind, tuple[str, ...]]:
    return {kind: () for kind in _KINDS}


def _ontology_samples(ontology: OntologyRegistry) -> list[tuple[OntologyKind, str, str]]:
    samples: list[tuple[OntologyKind, str, str]] = []
    definitions = (
        ("objects", ontology.objects),
        ("metrics", ontology.metrics),
        ("actions", ontology.actions),
    )
    for kind, values in definitions:
        for definition in values.values():
            aliases: dict[str, str] = {}
            for raw_alias in (definition.name, definition.label, *definition.keywords):
                alias = raw_alias.strip()
                if alias:
                    aliases.setdefault(alias.casefold(), alias)
            samples.extend((kind, definition.name, alias) for alias in aliases.values())
    return samples
