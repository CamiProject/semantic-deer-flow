"""Configuration for Ontology candidate recall during Semantic preflight."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SemanticRecallConfig(BaseModel):
    """Two-stage exact-string plus local FAISS Ontology recall configuration."""

    enabled: bool = False
    embedding_provider: Literal["hashing"] = "hashing"
    embedding_model: str = Field(default="semantic-hash-v1", min_length=1, max_length=128)
    embedding_dimension: int = Field(default=384, ge=32, le=4096)
    top_k: int = Field(default=12, ge=1, le=100)
    similarity_threshold: float = Field(default=0.55, ge=-1.0, le=1.0)
    min_votes: int = Field(default=1, ge=1, le=20)
    max_candidates_per_kind: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_vote_limit(self) -> "SemanticRecallConfig":
        if self.min_votes > self.top_k:
            raise ValueError("semantic_recall.min_votes cannot exceed top_k")
        return self
