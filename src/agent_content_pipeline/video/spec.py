from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VideoScriptSpec(BaseModel):
    """Agent-authored deterministic input; the core never fills missing prose."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_version: int = Field(default=1, alias="schemaVersion")
    narration: str = Field(min_length=1)
    material_terms: list[str] = Field(
        min_length=1,
        max_length=20,
        alias="materialTerms",
    )
