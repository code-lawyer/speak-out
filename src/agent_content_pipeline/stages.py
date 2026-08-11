from __future__ import annotations

from enum import StrEnum

from .social.models import SocialPlatform
from .state import PublicationRecordState


class PipelineStage(StrEnum):
    ARTICLE = "article"
    VIDEO = "video"
    SOCIAL_XIAOHONGSHU = "social:xiaohongshu"
    SOCIAL_DOUYIN = "social:douyin"
    SOCIAL_BILIBILI = "social:bilibili"

    @property
    def platform(self) -> SocialPlatform | None:
        if not self.value.startswith("social:"):
            return None
        return SocialPlatform(self.value.partition(":")[2])

    @property
    def publication_route(self) -> tuple[str, PublicationRecordState] | None:
        if self is PipelineStage.ARTICLE:
            return "website-wechat", PublicationRecordState.SUCCEEDED
        if self.platform is not None:
            return self.value, PublicationRecordState.SUBMITTED
        return None
