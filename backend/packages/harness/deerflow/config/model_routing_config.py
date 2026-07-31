"""Configuration for Gateway model routing."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class FaissRoutingConfig(BaseModel):
    """Local vector-search settings used by the second routing stage."""

    index_path: str = Field(
        default=".deer-flow/model-routing/routes.faiss",
        description="FAISS index path, relative to the DeerFlow project root unless absolute.",
    )
    examples_path: str = Field(
        default=".deer-flow/model-routing/route-examples.jsonl",
        description="Versioned routing examples JSONL path.",
    )
    metadata_path: str = Field(
        default=".deer-flow/model-routing/routes.meta.json",
        description="FAISS asset manifest containing index and sample compatibility metadata.",
    )
    embedding_provider: Literal["hashing"] = Field(
        default="hashing",
        description="Local embedding provider. The built-in hashing provider keeps the route module lightweight.",
    )
    embedding_model: str = Field(default="routing-hash-v1", description="Embedding provider version/name.")
    embedding_dimension: int = Field(default=384, ge=32, le=4096, description="Vector dimension used by the index.")
    top_k: int = Field(default=5, ge=1, le=50, description="Number of nearest examples to inspect.")
    similarity_threshold: float = Field(default=0.75, ge=-1.0, le=1.0, description="Minimum normalized inner-product score.")
    min_votes: int = Field(default=2, ge=1, le=50, description="Minimum agreeing neighbors required for a decision.")
    label_version: str = Field(default="1", min_length=1, max_length=128, description="Expected routing sample label version.")
    index_version: str = Field(default="1", min_length=1, max_length=128, description="Operator-managed FAISS index version.")

    @model_validator(mode="after")
    def _validate_vote_limit(self) -> "FaissRoutingConfig":
        if self.min_votes > self.top_k:
            raise ValueError("model_routing.faiss.min_votes cannot exceed top_k")
        return self


class ModelRoutingConfig(BaseModel):
    """Configuration for the two-stage simple/complex model router."""

    mode: Literal["disabled", "shadow", "enforce"] = Field(
        default="disabled",
        description="disabled keeps current behavior; shadow records decisions; enforce applies selected models.",
    )
    simple_model: str | None = Field(default=None, description="Configured model name for simple requests.")
    complex_model: str | None = Field(default=None, description="Configured model name for complex requests.")
    rules_version: str = Field(default="1", min_length=1, max_length=128, description="Operator-managed rule version.")
    faiss: FaissRoutingConfig = Field(default_factory=FaissRoutingConfig, description="Local FAISS fallback settings.")
