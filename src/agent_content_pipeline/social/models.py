from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SocialPlatform(StrEnum):
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    BILIBILI = "bilibili"


class SocialPostSpec(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    platform: SocialPlatform
    title: str = Field(min_length=1)
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    video_path: Path
    category: str | None = None

    @model_validator(mode="after")
    def validate_platform_limits(self) -> SocialPostSpec:
        limits = {
            SocialPlatform.XIAOHONGSHU: (20, 1000, 5),
            SocialPlatform.DOUYIN: (30, 1000, 5),
            SocialPlatform.BILIBILI: (79, 249, 10),
        }
        max_title, max_body, max_tags = limits[self.platform]
        if len(self.title) > max_title:
            raise ValueError(f"{self.platform.value} title exceeds {max_title} characters")
        if len(self.body) > max_body:
            raise ValueError(f"{self.platform.value} body exceeds {max_body} characters")
        if not 1 <= len(self.tags) <= max_tags:
            raise ValueError(
                f"{self.platform.value} requires 1-{max_tags} tags"
            )
        if self.platform == SocialPlatform.BILIBILI and not self.category:
            raise ValueError("bilibili category is required")
        if self.video_path.suffix.lower() != ".mp4":
            raise ValueError("the shared social video must be an MP4")
        return self


class SocialPublicationState(StrEnum):
    WAITING_FOR_USER = "waiting_for_user"
    SUBMITTED = "submitted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SocialUploadState(StrEnum):
    NOT_STARTED = "not_started"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class SocialPublishResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: SocialPlatform
    state: SocialPublicationState
    message: str
    permalink: str | None = None
    upload_state: SocialUploadState = SocialUploadState.NOT_STARTED
    submission_started: bool = False


class PlatformCopy(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    body: str = ""
    tags: list[str] = Field(min_length=1)
    category: str | None = None


class SocialCopyBundle(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    schema_version: int = Field(default=1, alias="schemaVersion")
    platforms: dict[SocialPlatform, PlatformCopy]

    @model_validator(mode="after")
    def require_all_initial_platforms(self) -> SocialCopyBundle:
        missing = set(SocialPlatform) - set(self.platforms)
        if missing:
            names = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"social copy is missing platforms: {names}")
        return self
