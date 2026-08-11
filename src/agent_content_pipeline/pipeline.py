from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .config import LocalConfig
from .orchestration import StageCommand
from .publishing.article import (
    ArticleValidationError,
    ArticlePublicationSpec,
    ArticlePublishPreview,
    ArticlePublishResult,
)
from .state import ApprovalLedger, ApprovalScope, PublicationLedger, PublicationRecordState
from .social.models import SocialPlatform
from .stages import PipelineStage
from .validation import validate_article_bundle
from .video.workflow import VideoRenderRequest, preflight_video_render
from .wechat import WeChatArticleRenderer, WeChatRenderError
from .workspace import (
    ArtifactIntegrityError,
    ArtifactKind,
    Product,
    ProductWorkspace,
)


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


class PipelinePlanningError(ValueError):
    """A user-correctable error in a requested multi-stage plan."""


class PipelinePlanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    stages: tuple[str, ...]
    article_revision: str | None = None
    cover_revision: str | None = None
    target_slug: str | None = None
    allow_duplicate_site: bool = False
    script_revision: str | None = None
    material_revision: str | None = None
    material_count: int | None = None
    bgm_directory: Path | None = None
    narration_audio: Path | None = None
    subtitles: Path | None = None
    allow_edge_tts_data_transfer: bool = False
    allow_pexels_data_transfer: bool = False
    video_revision: str | None = None
    copy_revision: str | None = None


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


def _stage_command_key(stage: str, args: tuple[str, ...]) -> str:
    encoded = json.dumps([stage, *args], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{stage}:{hashlib.sha256(encoded).hexdigest()}"


class PipelinePlanner:
    """Own stage selection, approval preflight, ordering, and retry blockers."""

    def __init__(
        self,
        *,
        workspace: ProductWorkspace,
        product: Product,
    ) -> None:
        self._workspace = workspace
        self._product = product
        self._approvals = ApprovalLedger(product.root)
        self._publications = PublicationLedger(product.root)

    def plan(self, request: PipelinePlanRequest) -> tuple[StageCommand, ...]:
        allowed = {item.value for item in PipelineStage}
        invalid = [stage for stage in request.stages if stage not in allowed]
        if invalid:
            raise PipelinePlanningError("unsupported stage: " + ", ".join(invalid))
        if not request.stages:
            raise PipelinePlanningError("at least one stage is required")
        return tuple(self._plan_stage(PipelineStage(stage), request) for stage in request.stages)

    def _plan_stage(
        self,
        stage: PipelineStage,
        request: PipelinePlanRequest,
    ) -> StageCommand:
        if stage is PipelineStage.ARTICLE:
            return self._plan_article(request)
        if stage is PipelineStage.VIDEO:
            return self._plan_video(request)
        return self._plan_social(stage, request)

    def _plan_article(self, request: PipelinePlanRequest) -> StageCommand:
        blockers: list[str] = []
        try:
            LocalConfig(request.project_root.resolve()).load()
        except Exception:
            blockers.append("website/WeChat production credentials are missing or invalid")
        if not request.article_revision or not request.cover_revision:
            raise PipelinePlanningError(
                "article stage requires article_revision and cover_revision"
            )
        destination_slug = request.target_slug or self._product.manifest.slug
        if destination_slug != self._product.manifest.slug and not request.allow_duplicate_site:
            raise PipelinePlanningError(
                "an override slug creates a duplicate website article; "
                "explicit allow_duplicate_site is required"
            )
        project_root = request.project_root.resolve()
        product_root = self._product.root.resolve()
        args = (
            "article",
            "publish",
            "--project-root",
            str(project_root),
            "--product",
            str(product_root),
            "--article-revision",
            request.article_revision,
            "--cover-revision",
            request.cover_revision,
        )
        if request.target_slug is not None:
            args += ("--target-slug", request.target_slug)
        if request.allow_duplicate_site:
            args += ("--allow-duplicate-site",)
        args += ("--execute", "--json")
        key = article_publication_approval_key(
            request.article_revision,
            request.cover_revision,
            destination_slug,
            True,
        )
        bundle = None
        try:
            bundle = load_article_publication_bundle(
                self._workspace,
                self._product,
                request.article_revision,
                request.cover_revision,
            )
        except ArtifactIntegrityError as error:
            blockers.extend(error.issues)
        except ArticleValidationError as error:
            blockers.extend(error.issues)
        if bundle is not None:
            requirements = article_publication_requirements(
                bundle,
                destination_slug,
                True,
            )
            blockers.extend(
                f"missing exact approval: {scope.value}:{revision}"
                for scope, revision, digest in requirements
                if not self._approvals.has(scope, revision, digest)
            )
        prior = self._publications.get_state("website-wechat", key)
        if prior == "succeeded":
            blockers.append("website/WeChat publication already succeeded")
        elif prior in {"partial", "unknown", "running"}:
            blockers.append(f"website/WeChat prior state is {prior}; reconcile before retry")
        return StageCommand(
            stage=PipelineStage.ARTICLE.value,
            idempotency_key=key,
            args=args,
            blockers=tuple(blockers),
        )

    def _plan_video(self, request: PipelinePlanRequest) -> StageCommand:
        blockers: list[str] = []
        if not request.script_revision:
            raise PipelinePlanningError("video stage requires script_revision")
        project_root = request.project_root.resolve()
        product_root = self._product.root.resolve()
        args = (
            "video",
            "render",
            "--project-root",
            str(project_root),
            "--product",
            str(product_root),
            "--script-revision",
            request.script_revision,
        )
        if request.material_revision is not None:
            args += ("--material-revision", request.material_revision)
        if request.material_count is not None:
            args += ("--material-count", str(request.material_count))
        if request.bgm_directory is not None:
            args += ("--bgm-directory", str(request.bgm_directory.resolve()))
        if request.narration_audio is not None:
            args += ("--narration-audio", str(request.narration_audio.resolve()))
        if request.subtitles is not None:
            args += ("--subtitles", str(request.subtitles.resolve()))
        if request.allow_edge_tts_data_transfer:
            args += ("--allow-edge-tts-data-transfer",)
        if request.allow_pexels_data_transfer:
            args += ("--allow-pexels-data-transfer",)
        args += ("--json",)
        key = _stage_command_key(PipelineStage.VIDEO.value, args)
        blockers.extend(
            preflight_video_render(
                workspace=self._workspace,
                product=self._product,
                approvals=self._approvals,
                request=VideoRenderRequest(
                    script_revision=request.script_revision,
                    material_revision=request.material_revision,
                    material_count=request.material_count,
                    bgm_directory=request.bgm_directory,
                    narration_audio=request.narration_audio,
                    subtitles=request.subtitles,
                    allow_edge_tts_data_transfer=request.allow_edge_tts_data_transfer,
                    allow_pexels_data_transfer=request.allow_pexels_data_transfer,
                ),
            ).issues
        )
        return StageCommand(
            stage=PipelineStage.VIDEO.value,
            idempotency_key=key,
            args=args,
            blockers=tuple(blockers),
        )

    def _plan_social(
        self,
        stage: PipelineStage,
        request: PipelinePlanRequest,
    ) -> StageCommand:
        if not request.video_revision or not request.copy_revision:
            raise PipelinePlanningError(
                f"{stage.value} requires video_revision and copy_revision"
            )
        platform = stage.platform
        assert platform is not None
        project_root = request.project_root.resolve()
        product_root = self._product.root.resolve()
        args = (
            "social",
            "publish",
            "--project-root",
            str(project_root),
            "--product",
            str(product_root),
            "--platform",
            platform.value,
            "--video-revision",
            request.video_revision,
            "--copy-revision",
            request.copy_revision,
            "--execute",
            "--json",
        )
        key = social_publication_approval_key(
            request.video_revision,
            request.copy_revision,
            platform,
        )
        blockers: list[str] = []
        from .social.workflow import (
            SocialPublicationRequest,
            SocialPublicationWorkflow,
            SocialPublicationWorkflowError,
        )

        try:
            SocialPublicationWorkflow(
                workspace=self._workspace,
                product=self._product,
                approvals=self._approvals,
                publications=self._publications,
            ).publish(
                SocialPublicationRequest(
                    platform=platform,
                    video_revision=request.video_revision,
                    copy_revision=request.copy_revision,
                    execute=False,
                )
            )
        except ArtifactIntegrityError as error:
            blockers.extend(error.issues)
        except SocialPublicationWorkflowError as error:
            blockers.append(str(error))
        except ValueError as error:
            blockers.append(f"social publication metadata is invalid: {error}")
        return StageCommand(
            stage=stage.value,
            idempotency_key=key,
            args=args,
            blockers=tuple(blockers),
        )

def article_publication_requirements(
    bundle: ArticlePublicationBundle,
    target_slug: str,
    push_to_wechat: bool,
) -> tuple[tuple[ApprovalScope, str, str], ...]:
    publication_key = article_publication_approval_key(
        bundle.article_revision,
        bundle.cover_revision,
        target_slug,
        push_to_wechat,
    )
    publication_digest = article_publication_content_digest(
        bundle.article_digest,
        bundle.cover_digest,
        target_slug,
        push_to_wechat,
    )
    return (
        (ApprovalScope.ARTICLE, bundle.article_revision, bundle.article_digest),
        (ApprovalScope.COVER, bundle.cover_revision, bundle.cover_digest),
        (ApprovalScope.ARTICLE_PUBLICATION, publication_key, publication_digest),
    )


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
        requirements = article_publication_requirements(
            bundle,
            spec.target_slug,
            spec.push_to_wechat,
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
