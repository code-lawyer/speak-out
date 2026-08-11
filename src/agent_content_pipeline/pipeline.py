from __future__ import annotations

import hashlib
import json
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .publishing.article import (
    ArticleValidationError,
    ArticlePublicationSpec,
    ArticlePublishPreview,
    ArticlePublishResult,
)
from .state import ApprovalLedger, ApprovalScope, PublicationLedger, PublicationRecordState
from .social.models import SocialPlatform
from .validation import validate_article_bundle
from .wechat import WeChatArticleRenderer, WeChatRenderError
from .workspace import ArtifactIntegrityError, ArtifactKind, Product, ProductWorkspace


class ArticlePublicationBundle(BaseModel):
    """One manifest-verified immutable article/cover byte snapshot."""

    model_config = ConfigDict(frozen=True)

    article_revision: str
    cover_revision: str
    article_digest: str
    cover_digest: str
    markdown: str
    body_html: str
    wechat_html: str
    cover_png: bytes
    wechat_layout_profile: str | None = None

    def specification(
        self,
        *,
        source_slug: str,
        target_slug: str,
        push_to_wechat: bool = True,
    ) -> ArticlePublicationSpec:
        return ArticlePublicationSpec(
            markdown=self.markdown,
            source_slug=source_slug,
            target_slug=target_slug,
            wechat_html=self.wechat_html,
            cover_png=self.cover_png,
            push_to_wechat=push_to_wechat,
        )


def load_article_publication_bundle(
    workspace: ProductWorkspace,
    product: Product,
    article_revision: str,
    cover_revision: str,
) -> ArticlePublicationBundle:
    integrity_issues: list[str] = []
    article = None
    cover = None
    try:
        article = workspace.read_verified_revision(
            product,
            ArtifactKind.ARTICLE,
            article_revision,
        )
    except ArtifactIntegrityError as error:
        integrity_issues.extend(error.issues)
    try:
        cover = workspace.read_verified_revision(
            product,
            ArtifactKind.COVER,
            cover_revision,
        )
    except ArtifactIntegrityError as error:
        integrity_issues.extend(error.issues)
    if integrity_issues:
        raise ArtifactIntegrityError(tuple(integrity_issues))
    assert article is not None and cover is not None
    try:
        markdown = article.files["article.mdx"].decode("utf-8")
        body_html = article.files["body.html"].decode("utf-8")
        wechat_html = article.files["index.html"].decode("utf-8")
        cover_png = cover.files["cover.png"]
    except (KeyError, UnicodeError) as error:
        raise ArticleValidationError(
            ("verified article bundle is incomplete or not UTF-8",)
        ) from error
    issues = validate_article_bundle(markdown, body_html, wechat_html, cover_png)
    layout_profile: str | None = None
    layout_bytes = article.files.get("wechat-layout.json")
    if layout_bytes is not None:
        try:
            layout = json.loads(layout_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            issues = (*issues, "wechat-layout.json is invalid")
        else:
            if layout != {
                "schemaVersion": 1,
                "profile": WeChatArticleRenderer.PROFILE,
            }:
                issues = (*issues, "wechat-layout.json uses an unsupported profile")
            else:
                layout_profile = WeChatArticleRenderer.PROFILE
            try:
                canonical = WeChatArticleRenderer().render(markdown)
            except (ValueError, WeChatRenderError) as error:
                issues = (*issues, f"deterministic WeChat rendering failed: {error}")
            else:
                if body_html != canonical.body_html:
                    issues = (*issues, "body.html does not match deterministic WeChat layout")
                if wechat_html != canonical.preview_html:
                    issues = (*issues, "index.html does not match deterministic WeChat layout")
    if issues:
        raise ArticleValidationError(issues)
    return ArticlePublicationBundle(
        article_revision=article_revision,
        cover_revision=cover_revision,
        article_digest=article.digest,
        cover_digest=cover.digest,
        markdown=markdown,
        body_html=body_html,
        wechat_html=wechat_html,
        wechat_layout_profile=layout_profile,
        cover_png=cover_png,
    )


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
        bundle = load_article_publication_bundle(
            self._workspace,
            self._product,
            article_revision,
            cover_revision,
        )
        destination_slug = target_slug or self._product.manifest.slug
        spec = bundle.specification(
            source_slug=self._product.manifest.slug,
            target_slug=destination_slug,
            push_to_wechat=push_to_wechat,
        )
        publication_key = article_publication_approval_key(
            article_revision,
            cover_revision,
            spec.target_slug,
            spec.push_to_wechat,
        )
        publication_digest = article_publication_content_digest(
            bundle.article_digest,
            bundle.cover_digest,
            spec.target_slug,
            spec.push_to_wechat,
        )
        requirements = (
            (ApprovalScope.ARTICLE, article_revision, bundle.article_digest),
            (ApprovalScope.COVER, cover_revision, bundle.cover_digest),
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
