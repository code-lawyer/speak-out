from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .publishing.article import (
    ArticleValidationError,
    ArticlePublicationSpec,
    ArticlePublishPreview,
    ArticlePublishResult,
)
from .state import ApprovalLedger, ApprovalScope, PublicationLedger, PublicationRecordState
from .social.models import SocialPlatform
from .validation import validate_article_bundle
from .workspace import ArtifactKind, Product, ProductWorkspace


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

    def __init__(
        self,
        approvals: ApprovalLedger,
        publisher: ArticlePublisher,
        workspace: ProductWorkspace,
        product: Product,
    ) -> None:
        self._approvals = approvals
        self._publisher = publisher
        self._workspace = workspace
        self._product = product
        self._publications = PublicationLedger(approvals.product_root)

    def publish(
        self,
        *,
        article_revision: str,
        cover_revision: str,
        target_slug: str | None = None,
        push_to_wechat: bool = True,
    ) -> ArticlePublishResult:
        article = self._workspace.read_verified_revision(
            self._product,
            ArtifactKind.ARTICLE,
            article_revision,
        )
        cover = self._workspace.read_verified_revision(
            self._product,
            ArtifactKind.COVER,
            cover_revision,
        )
        try:
            markdown = article.files["article.mdx"].decode("utf-8")
            body_html = article.files["body.html"].decode("utf-8")
            wechat_html = article.files["index.html"].decode("utf-8")
            cover_png = cover.files["cover.png"]
        except (KeyError, UnicodeError) as error:
            raise ArticleValidationError(
                ("verified article bundle is incomplete or not UTF-8",)
            ) from error
        validation_issues = validate_article_bundle(
            markdown,
            body_html,
            wechat_html,
            cover_png,
        )
        if validation_issues:
            raise ArticleValidationError(validation_issues)
        destination_slug = target_slug or self._product.manifest.slug
        spec = ArticlePublicationSpec(
            markdown=markdown,
            source_slug=self._product.manifest.slug,
            target_slug=destination_slug,
            wechat_html=wechat_html,
            cover_png=cover_png,
            push_to_wechat=push_to_wechat,
        )
        publication_key = article_publication_approval_key(
            article_revision,
            cover_revision,
            spec.target_slug,
            spec.push_to_wechat,
        )
        publication_digest = article_publication_content_digest(
            article.digest,
            cover.digest,
            spec.target_slug,
            spec.push_to_wechat,
        )
        requirements = (
            (ApprovalScope.ARTICLE, article_revision, article.digest),
            (ApprovalScope.COVER, cover_revision, cover.digest),
            (ApprovalScope.ARTICLE_PUBLICATION, publication_key, publication_digest),
        )
        missing = tuple(
            f"{scope.value}:{revision}"
            for scope, revision, digest in requirements
            if not self._approvals.has(scope, revision, digest)
        )
        if missing:
            raise ApprovalRequired(missing)

        destination = "website-wechat"
        attempt = self._publications.begin_attempt(destination, publication_key)
        prior_state = attempt.claim.prior_state
        if prior_state == "succeeded":
            raise AlreadyPublished(destination, publication_key)
        if not attempt.claim.acquired:
            raise UnsafeToRetry(destination, publication_key, prior_state)

        with attempt:
            preview = self._publisher.preview(spec)
            attempt.mark_external_started()
            result = self._publisher.publish(preview)
            attempt.finish(PublicationRecordState(result.state.value))
            return result
