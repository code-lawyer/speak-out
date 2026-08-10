from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .publishing.article import (
    ArticlePublicationSpec,
    ArticlePublishPreview,
    ArticlePublishResult,
)
from .state import ApprovalLedger, ApprovalScope, PublicationLedger
from .social.models import SocialPlatform


class ArticlePublisher(Protocol):
    def preview(self, spec: ArticlePublicationSpec) -> ArticlePublishPreview: ...

    def publish(self, preview: ArticlePublishPreview) -> ArticlePublishResult: ...


class ApprovalRequired(RuntimeError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("missing explicit approval: " + ", ".join(missing))


class AlreadyPublished(RuntimeError):
    def __init__(self, destination: str, idempotency_key: str) -> None:
        self.destination = destination
        self.idempotency_key = idempotency_key
        super().__init__(f"publication already succeeded: {destination}:{idempotency_key}")


class UnsafeToRetry(RuntimeError):
    def __init__(self, destination: str, idempotency_key: str, prior_state: str) -> None:
        self.destination = destination
        self.idempotency_key = idempotency_key
        self.prior_state = prior_state
        super().__init__(
            f"publication state {prior_state} must be reconciled before retry: "
            f"{destination}:{idempotency_key}"
        )


def article_publication_approval_key(
    article_revision: str,
    cover_revision: str,
    target_slug: str,
    push_to_wechat: bool,
) -> str:
    channel = "wechat" if push_to_wechat else "site"
    return f"{article_revision}+{cover_revision}+{target_slug}+{channel}"


def social_publication_approval_key(
    video_revision: str,
    copy_revision: str,
    platform: SocialPlatform,
) -> str:
    return f"{video_revision}+{copy_revision}+{platform.value}+publish"


def article_publication_content_digest(
    article_digest: str,
    cover_digest: str,
    target_slug: str,
    push_to_wechat: bool,
) -> str:
    return _approval_content_digest(
        "article-publication",
        article_digest,
        cover_digest,
        target_slug,
        "wechat" if push_to_wechat else "site",
    )


def social_publication_content_digest(
    video_digest: str,
    copy_digest: str,
    platform: SocialPlatform,
) -> str:
    return _approval_content_digest(
        "social-publication",
        video_digest,
        copy_digest,
        platform.value,
    )


def _approval_content_digest(*parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ArticlePublicationWorkflow:
    """Enforce durable approvals before crossing the ArticlePublisher seam."""

    def __init__(self, approvals: ApprovalLedger, publisher: ArticlePublisher) -> None:
        self._approvals = approvals
        self._publisher = publisher
        self._publications = PublicationLedger(approvals.product_root)

    def publish(
        self,
        spec: ArticlePublicationSpec,
        article_revision: str,
        cover_revision: str,
    ) -> ArticlePublishResult:
        publication_key = article_publication_approval_key(
            article_revision,
            cover_revision,
            spec.target_slug,
            spec.push_to_wechat,
        )
        requirements = (
            (ApprovalScope.ARTICLE, article_revision),
            (ApprovalScope.COVER, cover_revision),
            (ApprovalScope.ARTICLE_PUBLICATION, publication_key),
        )
        missing = tuple(
            f"{scope.value}:{revision}"
            for scope, revision in requirements
            if not self._approvals.has(scope, revision)
        )
        if missing:
            raise ApprovalRequired(missing)

        destination = "website-wechat"
        prior_state = self._publications.get_state(destination, publication_key)
        if prior_state == "succeeded":
            raise AlreadyPublished(destination, publication_key)
        if prior_state in {"partial", "unknown"}:
            raise UnsafeToRetry(destination, publication_key, prior_state)

        result = self._publisher.publish(self._publisher.preview(spec))
        self._publications.record_state(destination, publication_key, result.state.value)
        return result
