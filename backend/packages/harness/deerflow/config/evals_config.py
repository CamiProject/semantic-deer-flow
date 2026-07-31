"""Configuration for the one-shot offline Evals runner."""

from pydantic import BaseModel, Field


class EvalsEvidenceConfig(BaseModel):
    require_persistent_run_events: bool = True


class EvalsGateConfig(BaseModel):
    fail_on_any_p0: bool = True


class EvalsConfig(BaseModel):
    enabled: bool = False
    output_dir: str = ".deer-flow/evals/runs"
    max_concurrency: int = Field(default=3, ge=1, le=20)
    default_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    evidence: EvalsEvidenceConfig = Field(default_factory=EvalsEvidenceConfig)
    gate: EvalsGateConfig = Field(default_factory=EvalsGateConfig)
