from __future__ import annotations

import json
import hashlib
import random
import shutil
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from pydantic import ValidationError

from .config import LocalConfig
from .diagnostics import SystemDoctor
from .pipeline import (
    ArticlePublicationWorkflow,
    ArticlePublicationEvidence,
    article_publication_content_digest,
    article_publication_approval_key,
    social_publication_content_digest,
    social_publication_approval_key,
)
from .publishing.article import (
    ArticlePublicationSpec,
    FixedIpVpsPublisher,
    PublicationState,
    interpret_article_result,
    redact_publication_data,
    write_article_publish_log,
)
from .orchestration import (
    PipelineOrchestrator,
    ReconciliationNotAllowed,
    ReconciliationOutcome,
    RetryNotAllowed,
    StageCommand,
)
from .state import ApprovalLedger, ApprovalScope, PublicationLedger, StageAttemptLedger, StageState
from .validation import (
    validate_article_bundle,
    validate_article_documents,
    validate_png_cover,
)
from .video.spec import VideoScriptSpec
from .video.materials import PexelsMaterialSource
from .video.narration import EdgeTtsNarrator, NarrationResult
from .video.renderer import FfmpegExplainerRenderer
from .browser.cdp import CdpWebSocketClient, ChromePageController
from .browser.chrome import LocalChromeCdpDriver
from .social.browser_publishers import CONTRACTS, VisibleChromePlatformPublisher
from .social.models import (
    SocialCopyBundle,
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialPublishResult,
)
from .workspace import (
    ArtifactKind,
    ArtifactIntegrityError,
    ArtifactRevision,
    ArtifactRevisionRequest,
    Product,
    ProductCreateRequest,
    ProductWorkspace,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _verified_artifact(
    workspace: ProductWorkspace,
    product: Product,
    kind: ArtifactKind,
    revision: str,
) -> ArtifactRevision:
    try:
        return workspace.verify_revision(product, kind, revision)
    except ArtifactIntegrityError as error:
        raise typer.BadParameter(str(error), param_hint="--revision") from error


app = typer.Typer(
    name="acp",
    help="Create, render, and publish local content Products.",
    no_args_is_help=True,
)
product_app = typer.Typer(help="Create and inspect content Products.", no_args_is_help=True)
config_app = typer.Typer(help="Initialize and inspect local configuration.", no_args_is_help=True)
approval_app = typer.Typer(help="Record explicit user approvals.", no_args_is_help=True)
artifact_app = typer.Typer(help="Add immutable Product artifact revisions.", no_args_is_help=True)
article_app = typer.Typer(help="Preview and publish website/WeChat articles.", no_args_is_help=True)
video_app = typer.Typer(help="Acquire materials and render landscape explainer videos.", no_args_is_help=True)
social_app = typer.Typer(help="Approve and publish videos through visible local Chrome.", no_args_is_help=True)
app.add_typer(product_app, name="product")
app.add_typer(config_app, name="config")
app.add_typer(approval_app, name="approval")
app.add_typer(artifact_app, name="artifact")
app.add_typer(article_app, name="article")
app.add_typer(video_app, name="video")
app.add_typer(social_app, name="social")


@app.command("doctor")
def doctor(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    report = SystemDoctor(project_root=project_root).run()
    if json_output:
        typer.echo(report.model_dump_json())
    else:
        for check in report.checks:
            typer.echo(f"{'OK' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    if not report.ok:
        raise typer.Exit(code=1)


@config_app.command("init")
def init_config(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    secrets_path = LocalConfig(project_root).initialize()
    payload = {"ok": True, "secretsPath": str(secrets_path)}
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Created plaintext secret file: {secrets_path}")
    typer.echo("Keep this file out of Git and protect local backups.")


@config_app.command("migrate-legacy-article")
def migrate_legacy_article_config(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    legacy_push: Annotated[Path, typer.Option(help="Existing article-publisher push.mjs.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    path = LocalConfig(project_root).migrate_legacy_article_publisher(legacy_push)
    payload = {"ok": True, "secretsPath": str(path), "migrated": "website_wechat"}
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Migrated website/WeChat VPS settings into: {path}")
    typer.echo("No secret value was printed. The old Skill file was not modified.")


@article_app.command("preview")
def preview_article(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    article_revision: Annotated[str, typer.Option(help="Article revision.")],
    cover_revision: Annotated[str, typer.Option(help="Cover revision.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    _verified_artifact(workspace, product, ArtifactKind.ARTICLE, article_revision)
    _verified_artifact(workspace, product, ArtifactKind.COVER, cover_revision)
    article_root = product.root / "article" / article_revision
    cover_root = product.root / "cover" / cover_revision
    markdown = (article_root / "article.mdx").read_text(encoding="utf-8")
    body_html = (article_root / "body.html").read_text(encoding="utf-8")
    wechat_html = (article_root / "index.html").read_text(encoding="utf-8")
    cover_png = (cover_root / "cover.png").read_bytes()
    issues = validate_article_bundle(markdown, body_html, wechat_html, cover_png)
    if issues:
        raise typer.BadParameter("; ".join(issues), param_hint="--article-revision")

    website = LocalConfig(project_root).load().website_wechat
    publisher = FixedIpVpsPublisher(
        endpoint=website.endpoint,
        bearer_token=website.bearer_token.get_secret_value(),
        timeout_seconds=website.request_timeout_seconds,
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown=markdown,
            source_slug=product.manifest.slug,
            target_slug=product.manifest.slug,
            wechat_html=wechat_html,
            cover_png=cover_png,
            push_to_wechat=True,
        )
    )
    payload = {
        "ok": True,
        "mode": "dry-run",
        "endpoint": preview.endpoint,
        "sourceSlug": preview.source_slug,
        "slug": preview.target_slug,
        "site": "ready",
        "wechat": "ready",
        "cover": "ready",
        "duplicateSite": preview.duplicate_site,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Article ready for site and WeChat draft: {preview.target_slug}")
    typer.echo("No network request was sent.")


@article_app.command("approve-publication")
def approve_article_publication(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    article_revision: Annotated[str, typer.Option(help="Exact article revision.")],
    cover_revision: Annotated[str, typer.Option(help="Exact cover revision.")],
    target_slug: Annotated[str | None, typer.Option(help="Slug sent to the VPS.")] = None,
    allow_duplicate_site: Annotated[
        bool,
        typer.Option(
            "--allow-duplicate-site",
            help="Acknowledge that an override slug creates a duplicate website article.",
        ),
    ] = False,
    confirmed_by_user: Annotated[
        bool,
        typer.Option(
            "--confirmed-by-user",
            help="Assert that the user approved the exact preview and destination.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if not confirmed_by_user:
        raise typer.BadParameter("explicit --confirmed-by-user is required")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    article = _verified_artifact(
        workspace,
        product,
        ArtifactKind.ARTICLE,
        article_revision,
    )
    cover = _verified_artifact(workspace, product, ArtifactKind.COVER, cover_revision)
    validation_issues = validate_article_bundle(
        (article.root / "article.mdx").read_text(encoding="utf-8"),
        (article.root / "body.html").read_text(encoding="utf-8"),
        (article.root / "index.html").read_text(encoding="utf-8"),
        (cover.root / "cover.png").read_bytes(),
    )
    if validation_issues:
        raise typer.BadParameter(
            "; ".join(validation_issues),
            param_hint="--article-revision",
        )
    approvals = ApprovalLedger(product.root)
    missing_artifact_approvals = []
    if not approvals.has(ApprovalScope.ARTICLE, article_revision, article.digest):
        missing_artifact_approvals.append(f"article:{article_revision}")
    if not approvals.has(ApprovalScope.COVER, cover_revision, cover.digest):
        missing_artifact_approvals.append(f"cover:{cover_revision}")
    if missing_artifact_approvals:
        raise typer.BadParameter(
            "missing exact artifact approval: " + ", ".join(missing_artifact_approvals)
        )
    destination_slug = target_slug or product.manifest.slug
    if destination_slug != product.manifest.slug and not allow_duplicate_site:
        raise typer.BadParameter(
            "an override slug creates a duplicate website article; "
            "explicit --allow-duplicate-site is required"
        )
    key = article_publication_approval_key(
        article_revision,
        cover_revision,
        destination_slug,
        True,
    )
    publication_digest = article_publication_content_digest(
        article.digest,
        cover.digest,
        destination_slug,
        True,
    )
    approval = approvals.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        key,
        publication_digest,
    )
    payload = {
        "ok": True,
        "approval": {"scope": approval.scope.value, "revision": approval.revision},
        "sourceSlug": product.manifest.slug,
        "slug": destination_slug,
        "duplicateSite": destination_slug != product.manifest.slug,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Recorded article publication approval: {key}")


@article_app.command("publish")
def publish_article(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    article_revision: Annotated[str, typer.Option(help="Exact article revision.")],
    cover_revision: Annotated[str, typer.Option(help="Exact cover revision.")],
    target_slug: Annotated[str | None, typer.Option(help="Slug sent to the VPS.")] = None,
    allow_duplicate_site: Annotated[
        bool,
        typer.Option(
            "--allow-duplicate-site",
            help="Acknowledge that an override slug creates a duplicate website article.",
        ),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Send the approved bundle to the configured VPS."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    destination_slug = target_slug or product.manifest.slug
    if destination_slug != product.manifest.slug and not allow_duplicate_site:
        raise typer.BadParameter(
            "an override slug creates a duplicate website article; "
            "explicit --allow-duplicate-site is required"
        )

    article = _verified_artifact(
        workspace,
        product,
        ArtifactKind.ARTICLE,
        article_revision,
    )
    cover = _verified_artifact(workspace, product, ArtifactKind.COVER, cover_revision)
    article_root = article.root
    cover_root = cover.root
    markdown = (article_root / "article.mdx").read_text(encoding="utf-8")
    body_html = (article_root / "body.html").read_text(encoding="utf-8")
    wechat_html = (article_root / "index.html").read_text(encoding="utf-8")
    cover_png = (cover_root / "cover.png").read_bytes()
    issues = validate_article_bundle(markdown, body_html, wechat_html, cover_png)
    if issues:
        raise typer.BadParameter("; ".join(issues), param_hint="--article-revision")
    spec = ArticlePublicationSpec(
        markdown=markdown,
        source_slug=product.manifest.slug,
        target_slug=destination_slug,
        wechat_html=wechat_html,
        cover_png=cover_png,
        push_to_wechat=True,
    )
    website = LocalConfig(project_root).load().website_wechat
    publisher = FixedIpVpsPublisher(
        endpoint=website.endpoint,
        bearer_token=website.bearer_token.get_secret_value(),
        timeout_seconds=website.request_timeout_seconds,
    )
    preview = publisher.preview(spec)
    key = article_publication_approval_key(
        article_revision,
        cover_revision,
        destination_slug,
        True,
    )
    publication_digest = article_publication_content_digest(
        article.digest,
        cover.digest,
        destination_slug,
        True,
    )
    if not execute:
        approvals = ApprovalLedger(product.root)
        missing = [
            f"{scope.value}:{revision}"
            for scope, revision, digest in (
                (ApprovalScope.ARTICLE, article_revision, article.digest),
                (ApprovalScope.COVER, cover_revision, cover.digest),
                (ApprovalScope.ARTICLE_PUBLICATION, key, publication_digest),
            )
            if not approvals.has(scope, revision, digest)
        ]
        prior = PublicationLedger(product.root).get_state("website-wechat", key)
        blocked = bool(missing or prior in {"succeeded", "partial", "unknown", "running"})
        payload = {
            "ok": not blocked,
            "mode": "dry-run",
            "state": "blocked" if blocked else "planned",
            "endpoint": preview.endpoint,
            "sourceSlug": preview.source_slug,
            "slug": preview.target_slug,
            "site": "ready",
            "wechat": "ready",
            "cover": "ready",
            "missingApprovals": missing,
            "priorState": prior,
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo(f"Article publication: {payload['state']}")
            typer.echo("No network request was sent. Add --execute only after review.")
        return
    exact_approvals = ApprovalLedger(product.root)
    if not exact_approvals.has(ApprovalScope.ARTICLE, article_revision, article.digest):
        raise typer.BadParameter(f"exact article approval is missing: {article_revision}")
    if not exact_approvals.has(ApprovalScope.COVER, cover_revision, cover.digest):
        raise typer.BadParameter(f"exact cover approval is missing: {cover_revision}")
    if not exact_approvals.has(
        ApprovalScope.ARTICLE_PUBLICATION,
        key,
        publication_digest,
    ):
        raise typer.BadParameter("exact article publication approval is missing")
    result = ArticlePublicationWorkflow(ApprovalLedger(product.root), publisher).publish(
        spec,
        evidence=ArticlePublicationEvidence(
            article_revision=article_revision,
            article_digest=article.digest,
            cover_revision=cover_revision,
            cover_digest=cover.digest,
        ),
    )
    channels = interpret_article_result(result)
    log_path = write_article_publish_log(product.root, preview, result, channels)
    payload = {
        "ok": result.state == PublicationState.SUCCEEDED,
        "mode": "publish",
        "state": result.state.value,
        "httpStatus": result.http_status,
        "sourceSlug": preview.source_slug,
        "slug": preview.target_slug,
        "site": channels.site,
        "cover": channels.cover,
        "wechat": channels.wechat,
        "response": redact_publication_data(result.response),
        "error": result.error,
        "logFile": str(log_path.relative_to(product.root)),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"Website: {channels.site}")
        typer.echo(f"WeChat cover: {channels.cover}")
        typer.echo(f"WeChat draft: {channels.wechat}")
        typer.echo(f"Log: {log_path}")
    if result.state != PublicationState.SUCCEEDED:
        raise typer.Exit(code=1)


def _stage_command_key(stage: str, args: tuple[str, ...]) -> str:
    encoded = json.dumps([stage, *args], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"{stage}:{hashlib.sha256(encoded).hexdigest()}"


def _preflight_artifact(
    workspace: ProductWorkspace,
    product: Product,
    kind: ArtifactKind,
    revision: str,
    blockers: list[str],
) -> ArtifactRevision | None:
    try:
        return workspace.verify_revision(product, kind, revision)
    except ArtifactIntegrityError as error:
        blockers.extend(error.issues)
        return None


def _build_stage_commands(
    *,
    project_root: Path,
    product_root: Path,
    stages: list[str],
    article_revision: str | None,
    cover_revision: str | None,
    target_slug: str | None,
    allow_duplicate_site: bool,
    script_revision: str | None,
    material_revision: str | None,
    bgm_directory: Path | None,
    narration_audio: Path | None,
    subtitles: Path | None,
    allow_edge_tts_data_transfer: bool,
    allow_pexels_data_transfer: bool,
    video_revision: str | None,
    copy_revision: str | None,
) -> tuple[StageCommand, ...]:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    project_root = project_root.resolve()
    product_root = product.root.resolve()
    approvals = ApprovalLedger(product.root)
    publications = PublicationLedger(product.root)
    allowed = {
        "article",
        "video",
        "social:xiaohongshu",
        "social:douyin",
        "social:bilibili",
    }
    invalid = [stage for stage in stages if stage not in allowed]
    if invalid:
        raise typer.BadParameter("unsupported stage: " + ", ".join(invalid), param_hint="--stage")
    if not stages:
        raise typer.BadParameter("at least one --stage is required", param_hint="--stage")

    commands: list[StageCommand] = []
    for stage in stages:
        blockers: list[str] = []
        if stage == "article":
            if not article_revision or not cover_revision:
                raise typer.BadParameter(
                    "article stage requires --article-revision and --cover-revision",
                    param_hint="--stage article",
                )
            destination_slug = target_slug or product.manifest.slug
            if destination_slug != product.manifest.slug and not allow_duplicate_site:
                raise typer.BadParameter(
                    "an override slug creates a duplicate website article; "
                    "explicit --allow-duplicate-site is required"
                )
            args = (
                "article",
                "publish",
                "--project-root",
                str(project_root),
                "--product",
                str(product_root),
                "--article-revision",
                article_revision,
                "--cover-revision",
                cover_revision,
            )
            if target_slug is not None:
                args += ("--target-slug", target_slug)
            if allow_duplicate_site:
                args += ("--allow-duplicate-site",)
            args += ("--execute", "--json")
            key = article_publication_approval_key(
                article_revision,
                cover_revision,
                destination_slug,
                True,
            )
            article = _preflight_artifact(
                workspace,
                product,
                ArtifactKind.ARTICLE,
                article_revision,
                blockers,
            )
            cover = _preflight_artifact(
                workspace,
                product,
                ArtifactKind.COVER,
                cover_revision,
                blockers,
            )
            if article is not None and not approvals.has(
                ApprovalScope.ARTICLE,
                article_revision,
                article.digest,
            ):
                blockers.append(f"missing exact approval: article:{article_revision}")
            if cover is not None and not approvals.has(
                ApprovalScope.COVER,
                cover_revision,
                cover.digest,
            ):
                blockers.append(f"missing exact approval: cover:{cover_revision}")
            if article is not None and cover is not None:
                blockers.extend(
                    validate_article_bundle(
                        (article.root / "article.mdx").read_text(encoding="utf-8"),
                        (article.root / "body.html").read_text(encoding="utf-8"),
                        (article.root / "index.html").read_text(encoding="utf-8"),
                        (cover.root / "cover.png").read_bytes(),
                    )
                )
                publication_digest = article_publication_content_digest(
                    article.digest,
                    cover.digest,
                    destination_slug,
                    True,
                )
                if not approvals.has(
                    ApprovalScope.ARTICLE_PUBLICATION,
                    key,
                    publication_digest,
                ):
                    blockers.append(
                        "missing exact approval: article-publication:" + key
                    )
            prior_publication = publications.get_state("website-wechat", key)
            if prior_publication == "succeeded":
                blockers.append("website/WeChat publication already succeeded")
            elif prior_publication in {"partial", "unknown", "running"}:
                blockers.append(
                    f"website/WeChat prior state is {prior_publication}; reconcile before retry"
                )
        elif stage == "video":
            if not script_revision:
                raise typer.BadParameter(
                    "video stage requires --script-revision",
                    param_hint="--stage video",
                )
            args = (
                "video",
                "render",
                "--project-root",
                str(project_root),
                "--product",
                str(product_root),
                "--script-revision",
                script_revision,
            )
            if material_revision is not None:
                args += ("--material-revision", material_revision)
            if bgm_directory is not None:
                args += ("--bgm-directory", str(bgm_directory.resolve()))
            if narration_audio is not None:
                args += ("--narration-audio", str(narration_audio.resolve()))
            if subtitles is not None:
                args += ("--subtitles", str(subtitles.resolve()))
            if allow_edge_tts_data_transfer:
                args += ("--allow-edge-tts-data-transfer",)
            if allow_pexels_data_transfer:
                args += ("--allow-pexels-data-transfer",)
            args += ("--json",)
            key = _stage_command_key(stage, args)
            script = _preflight_artifact(
                workspace,
                product,
                ArtifactKind.VIDEO_SCRIPT,
                script_revision,
                blockers,
            )
            if script is not None and not approvals.has(
                ApprovalScope.VIDEO_SCRIPT,
                script_revision,
                script.digest,
            ):
                blockers.append(
                    f"missing exact approval: video-script:{script_revision}"
                )
            if (narration_audio is None) != (subtitles is None):
                blockers.append(
                    "local narration requires both --narration-audio and --subtitles"
                )
            local_narration = narration_audio is not None and subtitles is not None
            if local_narration:
                if not narration_audio.is_file():
                    blockers.append(f"local narration audio is missing: {narration_audio}")
                if not subtitles.is_file():
                    blockers.append(f"local subtitles are missing: {subtitles}")
            elif not allow_edge_tts_data_transfer:
                blockers.append("Edge TTS data-transfer approval is required")
            if material_revision is not None:
                material = _preflight_artifact(
                    workspace,
                    product,
                    ArtifactKind.VIDEO_MATERIAL,
                    material_revision,
                    blockers,
                )
                if material is not None and not any(
                    path.is_file()
                    and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
                    for path in material.root.iterdir()
                ):
                    blockers.append(
                        f"video-material revision contains no supported files: {material_revision}"
                    )
            elif not allow_pexels_data_transfer:
                blockers.append("Pexels data-transfer approval is required")
        else:
            if not video_revision or not copy_revision:
                raise typer.BadParameter(
                    f"{stage} requires --video-revision and --copy-revision",
                    param_hint=f"--stage {stage}",
                )
            platform = SocialPlatform(stage.split(":", 1)[1])
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
                video_revision,
                "--copy-revision",
                copy_revision,
                "--execute",
                "--json",
            )
            key = social_publication_approval_key(video_revision, copy_revision, platform)
            video = _preflight_artifact(
                workspace,
                product,
                ArtifactKind.VIDEO_RENDER,
                video_revision,
                blockers,
            )
            copy = _preflight_artifact(
                workspace,
                product,
                ArtifactKind.SOCIAL_COPY,
                copy_revision,
                blockers,
            )
            if video is not None and not approvals.has(
                ApprovalScope.VIDEO,
                video_revision,
                video.digest,
            ):
                blockers.append(f"missing exact approval: video:{video_revision}")
            if video is not None and copy is not None:
                publication_digest = social_publication_content_digest(
                    video.digest,
                    copy.digest,
                    platform,
                )
                if not approvals.has(
                    ApprovalScope.SOCIAL_PUBLICATION,
                    key,
                    publication_digest,
                ):
                    blockers.append(
                        "missing exact approval: social-publication:" + key
                    )
            prior_publication = publications.get_state(stage, key)
            if prior_publication == SocialPublicationState.SUBMITTED.value:
                blockers.append(f"{platform.value} publication already submitted")
            elif prior_publication in {
                SocialPublicationState.UNKNOWN.value,
                SocialPublicationState.WAITING_FOR_USER.value,
                "partial",
                "running",
            }:
                blockers.append(
                    f"{platform.value} prior state is {prior_publication}; "
                    "retry or reconcile before another submission"
                )
        commands.append(
            StageCommand(
                stage=stage,
                idempotency_key=key,
                args=args,
                blockers=tuple(blockers),
            )
        )
    return tuple(commands)


def _emit_pipeline_result(result, json_output: bool) -> None:
    if json_output:
        typer.echo(result.model_dump_json())
        return
    typer.echo(f"Mode: {result.mode}")
    for stage in result.stages:
        typer.echo(f"{stage.stage}: {stage.state.value}")
        for blocker in stage.output.get("blockers", []):
            typer.echo(f"  BLOCKED: {blocker}")
        if result.mode == "dry-run":
            typer.echo("  acp " + " ".join(stage.args))


@app.command("run")
def run_pipeline(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    stages: Annotated[
        list[str],
        typer.Option("--stage", help="article, video, or social:<platform>; repeatable."),
    ],
    article_revision: Annotated[str | None, typer.Option(help="Exact article revision.")] = None,
    cover_revision: Annotated[str | None, typer.Option(help="Exact cover revision.")] = None,
    target_slug: Annotated[str | None, typer.Option(help="Optional website target slug.")] = None,
    allow_duplicate_site: Annotated[
        bool,
        typer.Option("--allow-duplicate-site", help="Acknowledge a duplicate website article."),
    ] = False,
    script_revision: Annotated[str | None, typer.Option(help="Approved video script.")] = None,
    material_revision: Annotated[str | None, typer.Option(help="Local material revision.")] = None,
    bgm_directory: Annotated[Path | None, typer.Option(help="Optional BGM directory.")] = None,
    narration_audio: Annotated[
        Path | None,
        typer.Option(help="Optional local narration audio; requires --subtitles."),
    ] = None,
    subtitles: Annotated[
        Path | None,
        typer.Option(help="Optional local SRT; requires --narration-audio."),
    ] = None,
    allow_edge_tts_data_transfer: Annotated[
        bool,
        typer.Option(
            "--allow-edge-tts-data-transfer",
            help="Acknowledge that narration text will be sent to Microsoft Edge TTS.",
        ),
    ] = False,
    allow_pexels_data_transfer: Annotated[
        bool,
        typer.Option(
            "--allow-pexels-data-transfer",
            help="Acknowledge that material search terms will be sent to Pexels.",
        ),
    ] = False,
    video_revision: Annotated[str | None, typer.Option(help="Approved video revision.")] = None,
    copy_revision: Annotated[str | None, typer.Option(help="Exact social copy revision.")] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Actually execute the planned stages."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    commands = _build_stage_commands(
        project_root=project_root,
        product_root=product_root,
        stages=stages,
        article_revision=article_revision,
        cover_revision=cover_revision,
        target_slug=target_slug,
        allow_duplicate_site=allow_duplicate_site,
        script_revision=script_revision,
        material_revision=material_revision,
        bgm_directory=bgm_directory,
        narration_audio=narration_audio,
        subtitles=subtitles,
        allow_edge_tts_data_transfer=allow_edge_tts_data_transfer,
        allow_pexels_data_transfer=allow_pexels_data_transfer,
        video_revision=video_revision,
        copy_revision=copy_revision,
    )
    result = PipelineOrchestrator(StageAttemptLedger(product_root)).run(
        commands,
        execute=execute,
    )
    _emit_pipeline_result(result, json_output)
    if execute and any(
        stage.state not in {StageState.SUCCEEDED, StageState.SKIPPED}
        for stage in result.stages
    ):
        raise typer.Exit(code=1)


@app.command("retry")
def retry_pipeline_stage(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    stage: Annotated[str, typer.Option(help="Exact stage name to retry.")],
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Actually execute the safe retry."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    ProductWorkspace(product_root.parent).load(product_root)
    try:
        result = PipelineOrchestrator(StageAttemptLedger(product_root)).retry(
            stage,
            execute=execute,
        )
    except RetryNotAllowed as error:
        raise typer.BadParameter(str(error), param_hint="--stage") from error
    _emit_pipeline_result(result, json_output)
    if execute and result.stages[0].state != StageState.SUCCEEDED:
        raise typer.Exit(code=1)


@app.command("reconcile")
def reconcile_pipeline_stage(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    stage: Annotated[str, typer.Option(help="Exact unknown, running, or partial stage.")],
    outcome: Annotated[
        ReconciliationOutcome,
        typer.Option(help="Verified destination outcome: absent or succeeded."),
    ],
    evidence: Annotated[
        str,
        typer.Option(
            help="Local note describing how the destination was checked; no credentials."
        ),
    ],
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Append the reconciliation record."),
    ] = False,
    confirmed_by_user: Annotated[
        bool,
        typer.Option(
            "--confirmed-by-user",
            help="Assert the user personally confirmed the external evidence.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    ProductWorkspace(product_root.parent).load(product_root)
    if execute and not confirmed_by_user:
        raise typer.BadParameter(
            "explicit --confirmed-by-user is required to apply reconciliation"
        )
    try:
        result = PipelineOrchestrator(StageAttemptLedger(product_root)).reconcile(
            stage,
            outcome,
            evidence=evidence,
            execute=execute,
        )
    except ReconciliationNotAllowed as error:
        raise typer.BadParameter(str(error), param_hint="--stage") from error
    _emit_pipeline_result(result, json_output)


@approval_app.command("record")
def record_approval(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    scope: Annotated[ApprovalScope, typer.Option(help="Approval scope.")],
    revision: Annotated[str, typer.Option(help="Exact artifact revision, such as v001.")],
    confirmed_by_user: Annotated[
        bool,
        typer.Option(
            "--confirmed-by-user",
            help="Assert that the user explicitly approved this exact revision.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if not confirmed_by_user:
        raise typer.BadParameter("explicit --confirmed-by-user is required")
    artifact_scope = {
        ApprovalScope.ARTICLE: ArtifactKind.ARTICLE,
        ApprovalScope.COVER: ArtifactKind.COVER,
        ApprovalScope.VIDEO_SCRIPT: ArtifactKind.VIDEO_SCRIPT,
        ApprovalScope.VIDEO: ArtifactKind.VIDEO_RENDER,
    }
    if scope not in artifact_scope:
        raise typer.BadParameter(
            "use the dedicated publication approval command for publication scopes",
            param_hint="--scope",
        )
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    try:
        artifact = workspace.verify_revision(product, artifact_scope[scope], revision)
    except ArtifactIntegrityError as error:
        raise typer.BadParameter(str(error), param_hint="--revision") from error
    approval = ApprovalLedger(product_root).record(scope, revision, artifact.digest)
    payload = {
        "ok": True,
        "approval": {"scope": approval.scope.value, "revision": approval.revision},
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Recorded {approval.scope.value} approval for {approval.revision}")


@artifact_app.command("add-article")
def add_article_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    mdx: Annotated[Path, typer.Option(help="Final website MDX file.")],
    body_html: Annotated[Path, typer.Option(help="Validated WeChat body HTML.")],
    wechat_html: Annotated[Path, typer.Option(help="Complete WeChat HTML.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    mdx_bytes = mdx.read_bytes()
    body_bytes = body_html.read_bytes()
    wechat_bytes = wechat_html.read_bytes()
    mdx_text = mdx_bytes.decode("utf-8")
    body_text = body_bytes.decode("utf-8")
    wechat_text = wechat_bytes.decode("utf-8")
    issues = validate_article_documents(mdx_text, body_text, wechat_text)
    if issues:
        raise typer.BadParameter("; ".join(issues), param_hint="--mdx")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={
                "article.mdx": mdx_bytes,
                "body.html": body_bytes,
                "index.html": wechat_bytes,
            },
        ),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added article {revision.revision}: {revision.root}")


@artifact_app.command("add-source")
def add_source_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    sources: Annotated[
        list[Path],
        typer.Option("--source", help="Source note or input file; repeatable."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    files: dict[str, bytes] = {}
    for source in sources:
        if not source.is_file():
            raise typer.BadParameter(f"source file is missing: {source}", param_hint="--source")
        if source.name in files:
            raise typer.BadParameter(
                f"source filenames must be unique: {source.name}",
                param_hint="--source",
            )
        files[source.name] = source.read_bytes()
    if not files:
        raise typer.BadParameter("at least one --source is required", param_hint="--source")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(kind=ArtifactKind.SOURCE, files=files),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added source notes {revision.revision}: {revision.root}")


@artifact_app.command("seal-legacy")
def seal_legacy_artifact_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    kind: Annotated[ArtifactKind, typer.Option(help="Existing artifact kind.")],
    revision: Annotated[str, typer.Option(help="Existing legacy revision.")],
    confirmed_by_user: Annotated[
        bool,
        typer.Option(
            "--confirmed-by-user",
            help="Confirm that the current local bytes are the intended migration baseline.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if not confirmed_by_user:
        raise typer.BadParameter("explicit --confirmed-by-user is required")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    try:
        artifact = workspace.seal_legacy_revision(product, kind, revision)
    except ArtifactIntegrityError as error:
        raise typer.BadParameter(str(error), param_hint="--revision") from error
    payload = {
        "ok": True,
        "artifact": {
            "kind": artifact.kind.value,
            "revision": artifact.revision,
            "root": str(artifact.root),
            "digest": artifact.digest,
        },
        "approvalRecorded": False,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Sealed legacy {kind.value}:{revision} without recording approval.")


@artifact_app.command("add-cover")
def add_cover_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    cover: Annotated[Path, typer.Option(help="Landscape PNG cover.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    cover_bytes = cover.read_bytes()
    issues = validate_png_cover(cover_bytes)
    if issues:
        raise typer.BadParameter("; ".join(issues), param_hint="--cover")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": cover_bytes},
        ),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added cover {revision.revision}: {revision.root}")


@artifact_app.command("add-video-script")
def add_video_script_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    script: Annotated[Path, typer.Option(help="Agent-authored video script JSON.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        spec = VideoScriptSpec.model_validate_json(script.read_text(encoding="utf-8"))
    except ValidationError as error:
        raise typer.BadParameter(str(error), param_hint="--script") from error
    content = (
        json.dumps(spec.model_dump(by_alias=True), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_SCRIPT,
            files={"script.json": content},
        ),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added video script {revision.revision}: {revision.root}")


@artifact_app.command("add-video-material")
def add_video_material_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    materials: Annotated[
        list[Path],
        typer.Option("--material", help="Local video material; repeat for multiple files."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    allowed_suffixes = {".mp4", ".mov", ".mkv", ".webm"}
    files: dict[str, bytes] = {}
    for index, material in enumerate(materials, start=1):
        suffix = material.suffix.lower()
        if suffix not in allowed_suffixes:
            raise typer.BadParameter(
                f"unsupported video material extension: {suffix}",
                param_hint="--material",
            )
        files[f"{index:03d}{suffix}"] = material.read_bytes()
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(kind=ArtifactKind.VIDEO_MATERIAL, files=files),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added video materials {revision.revision}: {revision.root}")


@artifact_app.command("add-social-copy")
def add_social_copy_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    copy: Annotated[Path, typer.Option(help="Agent-authored copy JSON for all platforms.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    try:
        bundle = SocialCopyBundle.model_validate_json(copy.read_text(encoding="utf-8"))
        for platform, platform_copy in bundle.platforms.items():
            SocialPostSpec(
                platform=platform,
                title=platform_copy.title,
                body=platform_copy.body,
                tags=platform_copy.tags,
                video_path=Path("final.mp4"),
                category=platform_copy.category,
            )
    except ValidationError as error:
        raise typer.BadParameter(str(error), param_hint="--copy") from error
    content = (
        json.dumps(bundle.model_dump(by_alias=True, mode="json"), ensure_ascii=False, indent=2)
        + "\n"
    ).encode("utf-8")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.SOCIAL_COPY,
            files={"copy.json": content},
        ),
    )
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Added social copy {revision.revision}: {revision.root}")


@video_app.command("render")
def render_video(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    script_revision: Annotated[str, typer.Option(help="Approved video-script revision.")],
    material_revision: Annotated[
        str | None,
        typer.Option(help="Local video-material revision; omit to acquire from Pexels."),
    ] = None,
    bgm_directory: Annotated[
        Path | None,
        typer.Option(help="Optional directory; one audio file is selected randomly."),
    ] = None,
    narration_audio: Annotated[
        Path | None,
        typer.Option(help="Local narration audio; use together with --subtitles."),
    ] = None,
    subtitles: Annotated[
        Path | None,
        typer.Option(help="Local SRT subtitles; use together with --narration-audio."),
    ] = None,
    allow_edge_tts_data_transfer: Annotated[
        bool,
        typer.Option(
            "--allow-edge-tts-data-transfer",
            help="Acknowledge sending the approved narration text to Microsoft Edge TTS.",
        ),
    ] = False,
    allow_pexels_data_transfer: Annotated[
        bool,
        typer.Option(
            "--allow-pexels-data-transfer",
            help="Acknowledge sending material search terms to Pexels.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    script_artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.VIDEO_SCRIPT,
        script_revision,
    )
    if not ApprovalLedger(product.root).has(
        ApprovalScope.VIDEO_SCRIPT,
        script_revision,
        script_artifact.digest,
    ):
        raise typer.BadParameter(
            f"explicit video-script approval is required for {script_revision}"
        )
    script_path = script_artifact.root / "script.json"
    spec = VideoScriptSpec.model_validate_json(script_path.read_text(encoding="utf-8"))
    settings = LocalConfig(project_root).load()
    if (narration_audio is None) != (subtitles is None):
        raise typer.BadParameter(
            "--narration-audio and --subtitles must be supplied together"
        )
    local_narration = narration_audio is not None and subtitles is not None
    if local_narration:
        if not narration_audio.is_file():
            raise typer.BadParameter(f"local narration audio is missing: {narration_audio}")
        if not subtitles.is_file():
            raise typer.BadParameter(f"local subtitles are missing: {subtitles}")
    elif not allow_edge_tts_data_transfer:
        raise typer.BadParameter(
            "Edge TTS sends the approved narration text to Microsoft; explicit "
            "--allow-edge-tts-data-transfer is required, or provide local "
            "--narration-audio and --subtitles"
        )
    if material_revision is None and not allow_pexels_data_transfer:
        raise typer.BadParameter(
            "Pexels receives the approved material search terms; explicit "
            "--allow-pexels-data-transfer is required, or provide --material-revision"
        )
    staging = product.root / "video" / "work" / f"render-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)

    if material_revision is not None:
        material_artifact = _verified_artifact(
            workspace,
            product,
            ArtifactKind.VIDEO_MATERIAL,
            material_revision,
        )
        material_root = material_artifact.root
        material_paths = sorted(
            path
            for path in material_root.iterdir()
            if path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
        )
        if not material_paths:
            raise typer.BadParameter(
                f"video-material revision contains no supported files: {material_revision}"
            )
    else:
        api_key = settings.pexels.api_key.get_secret_value()
        if not api_key:
            raise typer.BadParameter(
                "Pexels API key is missing; edit .local/secrets.toml or pass --material-revision"
            )
        downloads = PexelsMaterialSource(api_key=api_key).acquire(
            terms=spec.material_terms,
            destination=staging / "materials",
            max_files=min(6, max(1, len(spec.material_terms))),
        )
        material_paths = [item.path for item in downloads]

    bgm: Path | None = None
    if bgm_directory is not None:
        candidates = sorted(
            path
            for path in bgm_directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"}
        )
        if not candidates:
            raise typer.BadParameter(f"BGM directory contains no supported audio: {bgm_directory}")
        bgm = random.SystemRandom().choice(candidates)

    if local_narration:
        narration_root = staging / "narration"
        narration_root.mkdir(parents=True)
        local_audio = narration_root / f"narration{narration_audio.suffix.lower()}"
        local_subtitles = narration_root / "subtitles.srt"
        shutil.copy2(narration_audio, local_audio)
        shutil.copy2(subtitles, local_subtitles)
        (narration_root / "script.txt").write_text(
            spec.narration + "\n",
            encoding="utf-8",
        )
        narration = NarrationResult(
            root=narration_root,
            audio_path=local_audio,
            subtitles_path=local_subtitles,
            voice="local",
        )
    else:
        narration = EdgeTtsNarrator(
            timeout_seconds=settings.tts.request_timeout_seconds
        ).synthesize(
            narration=spec.narration,
            voice=settings.tts.voice,
            output_root=staging / "narration",
        )
    rendered = FfmpegExplainerRenderer().render_from_assets(
        materials=material_paths,
        narration_audio=narration.audio_path,
        subtitles=narration.subtitles_path,
        output_root=staging / "output",
        bgm=bgm,
    )
    (staging / "workflow.json").write_text(
        json.dumps(
            {
                "scriptRevision": script_revision,
                "materialRevision": material_revision,
                "voice": narration.voice,
                "narrationSource": "local" if local_narration else "edge-tts",
                "bgm": str(bgm) if bgm else None,
                "profile": "landscape-explainer-v1",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    revision = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        staging,
    )
    final_path = revision.root / "output" / rendered.video_path.name
    payload = {
        "ok": True,
        "artifact": {
            "kind": revision.kind.value,
            "revision": revision.revision,
            "root": str(revision.root),
        },
        "video": {
            "path": str(final_path),
            "width": rendered.width,
            "height": rendered.height,
            "codec": rendered.video_codec,
            "hasAudio": rendered.has_audio,
            "durationSeconds": rendered.duration_seconds,
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Rendered video {revision.revision}: {final_path}")


@video_app.command("inspect")
def inspect_video_revision(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    revision: Annotated[str, typer.Option(help="Exact video-render revision.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.VIDEO_RENDER,
        revision,
    )
    video_path = artifact.root / "output" / "final.mp4"
    if not video_path.is_file():
        raise typer.BadParameter(f"video revision is missing final.mp4: {revision}")
    inspected = FfmpegExplainerRenderer().inspect(video_path)
    payload = {
        "ok": True,
        "revision": revision,
        "video": {
            "path": str(video_path),
            "width": inspected.width,
            "height": inspected.height,
            "codec": inspected.video_codec,
            "hasAudio": inspected.has_audio,
            "durationSeconds": inspected.duration_seconds,
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Video {revision}: {video_path}")
    typer.echo(
        f"{inspected.width}x{inspected.height} {inspected.video_codec}, "
        f"audio={'yes' if inspected.has_audio else 'no'}, "
        f"duration={inspected.duration_seconds:.3f}s"
    )


@social_app.command("preview")
def preview_social_publication(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    platform: Annotated[SocialPlatform, typer.Option(help="Target platform.")],
    video_revision: Annotated[str, typer.Option(help="Exact video-render revision.")],
    copy_revision: Annotated[str, typer.Option(help="Exact social-copy revision.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    video = _verified_artifact(
        workspace,
        product,
        ArtifactKind.VIDEO_RENDER,
        video_revision,
    )
    copy_artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.SOCIAL_COPY,
        copy_revision,
    )
    video_path = video.root / "output" / "final.mp4"
    if not video_path.is_file():
        raise typer.BadParameter(f"video revision is missing final.mp4: {video_revision}")
    bundle = SocialCopyBundle.model_validate_json(
        (copy_artifact.root / "copy.json").read_text(encoding="utf-8")
    )
    platform_copy = bundle.platforms[platform]
    SocialPostSpec(
        platform=platform,
        title=platform_copy.title,
        body=platform_copy.body,
        tags=platform_copy.tags,
        video_path=video_path,
        category=platform_copy.category,
    )
    key = social_publication_approval_key(video_revision, copy_revision, platform)
    publication_digest = social_publication_content_digest(
        video.digest,
        copy_artifact.digest,
        platform,
    )
    approvals = ApprovalLedger(product.root)
    payload = {
        "ok": True,
        "mode": "dry-run",
        "target": "immediate-publication",
        "platform": platform.value,
        "video": str(video_path),
        "videoRevision": video_revision,
        "copyRevision": copy_revision,
        "title": platform_copy.title,
        "body": platform_copy.body,
        "tags": platform_copy.tags,
        "category": platform_copy.category,
        "videoApproved": approvals.has(ApprovalScope.VIDEO, video_revision, video.digest),
        "publicationApproved": approvals.has(
            ApprovalScope.SOCIAL_PUBLICATION,
            key,
            publication_digest,
        ),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Target: immediate {platform.value} publication")
    typer.echo(f"Title: {platform_copy.title}")
    typer.echo(f"Body: {platform_copy.body}")
    typer.echo("Tags: " + ", ".join(platform_copy.tags))
    typer.echo(f"Video: {video_path}")
    typer.echo("No browser was opened and nothing was submitted.")


@social_app.command("approve-publication")
def approve_social_publication(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    platform: Annotated[SocialPlatform, typer.Option(help="Target platform.")],
    video_revision: Annotated[str, typer.Option(help="Approved video-render revision.")],
    copy_revision: Annotated[str, typer.Option(help="Exact social-copy revision.")],
    confirmed_by_user: Annotated[
        bool,
        typer.Option(
            "--confirmed-by-user",
            help="Assert that the user approved immediate publication to this platform.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    if not confirmed_by_user:
        raise typer.BadParameter("explicit --confirmed-by-user is required")
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    video = _verified_artifact(
        workspace,
        product,
        ArtifactKind.VIDEO_RENDER,
        video_revision,
    )
    copy_artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.SOCIAL_COPY,
        copy_revision,
    )
    approvals = ApprovalLedger(product.root)
    if not approvals.has(ApprovalScope.VIDEO, video_revision, video.digest):
        raise typer.BadParameter(
            f"the exact video must be approved first: video:{video_revision}"
        )
    key = social_publication_approval_key(video_revision, copy_revision, platform)
    publication_digest = social_publication_content_digest(
        video.digest,
        copy_artifact.digest,
        platform,
    )
    approval = approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        key,
        publication_digest,
    )
    payload = {
        "ok": True,
        "platform": platform.value,
        "approval": {"scope": approval.scope.value, "revision": approval.revision},
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Recorded immediate {platform.value} publication approval: {key}")


@social_app.command("login")
def open_social_login(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    platform: Annotated[SocialPlatform, typer.Option(help="Platform to open.")] = SocialPlatform.BILIBILI,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    settings = LocalConfig(project_root).load()
    configured_chrome = Path(settings.browser.chrome_path) if settings.browser.chrome_path else None
    contract = CONTRACTS[platform]
    typer.echo(
        (
            f"Opening visible {platform.value} Chrome. Do not give passwords to the Agent; "
            "complete QR code, captcha, or login directly in the window. Login state is retained "
            f"only in .local/browser-profiles/{platform.value}."
        ),
        err=True,
    )
    session = LocalChromeCdpDriver(
        project_root=project_root,
        chrome_path=configured_chrome,
    ).launch(platform=platform.value, start_url=contract.upload_url)
    payload = {
        "ok": True,
        "platform": platform.value,
        "profile": str(session.profile_root),
        "browser": "open",
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Chrome profile opened: {session.profile_root}")


@social_app.command("status")
def social_login_status(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    platform: Annotated[SocialPlatform, typer.Option(help="Platform to inspect.")] = SocialPlatform.BILIBILI,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    settings = LocalConfig(project_root).load()
    configured_chrome = Path(settings.browser.chrome_path) if settings.browser.chrome_path else None
    contract = CONTRACTS[platform]
    session = LocalChromeCdpDriver(
        project_root=project_root,
        chrome_path=configured_chrome,
    ).launch(platform=platform.value, start_url=contract.upload_url)
    cdp = CdpWebSocketClient(session.websocket_url)
    try:
        page = ChromePageController.attach(cdp)
        page.navigate(contract.upload_url)
        deadline = time.monotonic() + 20
        state = "unknown"
        while time.monotonic() < deadline:
            if any(page.exists(selector) for selector in contract.file_inputs):
                state = "ready"
                break
            if any(page.exists(selector) for selector in contract.login_markers) or page.evaluate(
                "(() => { const text = document.body?.innerText || ''; "
                "return text.includes('登录') || text.includes('扫码'); })()"
            ):
                state = "login_required"
                break
            time.sleep(0.5)
    finally:
        cdp.close()
    payload = {
        "ok": state == "ready",
        "platform": platform.value,
        "state": state,
        "profile": str(session.profile_root),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"{platform.value}: {state}")


@social_app.command("close")
def close_social_browser(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    platform: Annotated[SocialPlatform, typer.Option(help="Platform profile to close.")] = SocialPlatform.BILIBILI,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    settings = LocalConfig(project_root).load()
    configured_chrome = Path(settings.browser.chrome_path) if settings.browser.chrome_path else None
    driver = LocalChromeCdpDriver(
        project_root=project_root,
        chrome_path=configured_chrome,
    )
    session = driver.connect_existing(platform=platform.value)
    state = "already-closed"
    if session is not None:
        driver.close(session)
        state = "closed"
    payload = {
        "ok": True,
        "platform": platform.value,
        "profile": str(project_root / ".local" / "browser-profiles" / platform.value),
        "browser": state,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"{platform.value}: {state}")


@social_app.command("publish")
def publish_social_video(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")],
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    platform: Annotated[SocialPlatform, typer.Option(help="Target platform.")],
    video_revision: Annotated[str, typer.Option(help="Approved video-render revision.")],
    copy_revision: Annotated[str, typer.Option(help="Approved social-copy revision.")],
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Open Chrome and submit the approved publication."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    video_artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.VIDEO_RENDER,
        video_revision,
    )
    copy_artifact = _verified_artifact(
        workspace,
        product,
        ArtifactKind.SOCIAL_COPY,
        copy_revision,
    )
    approvals = ApprovalLedger(product.root)
    key = social_publication_approval_key(video_revision, copy_revision, platform)
    publication_digest = social_publication_content_digest(
        video_artifact.digest,
        copy_artifact.digest,
        platform,
    )
    missing = []
    if not approvals.has(ApprovalScope.VIDEO, video_revision, video_artifact.digest):
        missing.append(f"video:{video_revision}")
    if not approvals.has(ApprovalScope.SOCIAL_PUBLICATION, key, publication_digest):
        missing.append(f"social-publication:{key}")
    if missing:
        raise typer.BadParameter("missing explicit approval: " + ", ".join(missing))

    publications = PublicationLedger(product.root)
    destination = f"social:{platform.value}"
    prior = publications.get_state(destination, key)
    if prior == SocialPublicationState.SUBMITTED.value:
        raise typer.BadParameter(f"publication already submitted: {destination}:{key}")
    if prior in {
        SocialPublicationState.UNKNOWN.value,
        "partial",
        "running",
    }:
        raise typer.BadParameter(
            f"prior publication state is {prior}; reconcile before retrying: "
            f"{destination}:{key}"
        )

    video_path = video_artifact.root / "output" / "final.mp4"
    copy_path = copy_artifact.root / "copy.json"
    bundle = SocialCopyBundle.model_validate_json(copy_path.read_text(encoding="utf-8"))
    platform_copy = bundle.platforms[platform]
    spec = SocialPostSpec(
        platform=platform,
        title=platform_copy.title,
        body=platform_copy.body,
        tags=platform_copy.tags,
        video_path=video_path,
        category=platform_copy.category,
    )
    if not video_path.is_file():
        raise typer.BadParameter(f"approved video file is missing: {video_path}")

    if not execute:
        payload = {
            "ok": True,
            "mode": "dry-run",
            "state": "planned",
            "platform": platform.value,
            "video": str(video_path),
            "title": spec.title,
            "body": spec.body,
            "tags": spec.tags,
            "category": spec.category,
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo(f"{platform.value}: planned")
            typer.echo("Chrome was not opened. Add --execute only after review.")
        return

    settings = LocalConfig(project_root).load()
    configured_chrome = Path(settings.browser.chrome_path) if settings.browser.chrome_path else None
    contract = CONTRACTS[platform]
    typer.echo(
        (
            f"Opening visible {platform.value} Chrome profile. "
            "If login is required, complete it in the window; login state remains only in "
            f".local/browser-profiles/{platform.value}."
        ),
        err=True,
    )
    driver = LocalChromeCdpDriver(
        project_root=project_root,
        chrome_path=configured_chrome,
    )
    session = driver.launch(platform=platform.value, start_url=contract.upload_url)
    cdp = CdpWebSocketClient(session.websocket_url)
    timestamp = datetime.now(UTC)
    screenshot_path: Path | None = None
    page = None
    result = SocialPublishResult(
        platform=platform,
        state=SocialPublicationState.UNKNOWN,
        message="browser automation did not complete; reconcile before retrying",
    )
    try:
        attempt = publications.begin_attempt(destination, key)
        if not attempt.claim.acquired:
            raise typer.BadParameter(
                f"publication is already claimed with state {attempt.claim.prior_state}: "
                f"{destination}:{key}"
            )
        with attempt:
            try:
                page = ChromePageController.attach(cdp)
            except Exception as error:
                result = SocialPublishResult(
                    platform=platform,
                    state=SocialPublicationState.FAILED,
                    message=(
                        "the local creator page could not be attached before publication; "
                        f"this attempt is safe to retry ({type(error).__name__})"
                    ),
                )
            else:
                attempt.mark_external_started()
                try:
                    result = VisibleChromePlatformPublisher(platform).publish(page, spec)
                except Exception as error:
                    result = SocialPublishResult(
                        platform=platform,
                        state=SocialPublicationState.UNKNOWN,
                        message=(
                            "browser automation was interrupted after opening the creator flow; "
                            f"reconcile before retrying ({type(error).__name__})"
                        ),
                    )
            attempt.finish(result.state.value)
    finally:
        if page is not None and result.state != SocialPublicationState.SUBMITTED:
            candidate = product.root / "logs" / (
                timestamp.isoformat().replace(":", "-").replace(".", "-")
                + f"-{platform.value}.png"
            )
            try:
                screenshot_path = page.screenshot(candidate)
            except Exception:
                screenshot_path = None
        try:
            cdp.close()
        except Exception:
            pass

    log_root = product.root / "logs"
    log_root.mkdir(exist_ok=True)
    log_path = log_root / (
        timestamp.isoformat().replace(":", "-").replace(".", "-")
        + f"-{platform.value}.json"
    )
    log_path.write_text(
        json.dumps(
            {
                "timestamp": timestamp.isoformat(),
                "destination": destination,
                "idempotencyKey": key,
                "videoRevision": video_revision,
                "copyRevision": copy_revision,
                "platform": platform.value,
                "state": result.state.value,
                "message": result.message,
                "permalink": result.permalink,
                "screenshotFile": (
                    str(screenshot_path.relative_to(product.root))
                    if screenshot_path is not None
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    payload = {
        "ok": result.state == SocialPublicationState.SUBMITTED,
        "platform": platform.value,
        "state": result.state.value,
        "message": result.message,
        "permalink": result.permalink,
        "logFile": str(log_path.relative_to(product.root)),
        "screenshotFile": (
            str(screenshot_path.relative_to(product.root))
            if screenshot_path is not None
            else None
        ),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"{platform.value}: {result.state.value} — {result.message}")
        typer.echo(f"Log: {log_path}")
    if result.state != SocialPublicationState.SUBMITTED:
        raise typer.Exit(code=3 if result.state == SocialPublicationState.WAITING_FOR_USER else 1)


@product_app.command("create")
def create_product(
    title: Annotated[str, typer.Option(help="Human-readable Product title.")],
    slug: Annotated[str, typer.Option(help="Lowercase, hyphenated Product slug.")],
    created_on: Annotated[str, typer.Option("--date", help="Creation date (YYYY-MM-DD).")],
    workspace: Annotated[Path, typer.Option(help="Workspace root.")] = Path("workspace"),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    product = ProductWorkspace(workspace).create(
        ProductCreateRequest(
            title=title,
            slug=slug,
            created_on=date.fromisoformat(created_on),
        )
    )
    payload = {
        "ok": True,
        "product": {
            "id": product.manifest.product_id,
            "title": product.manifest.title,
            "slug": product.manifest.slug,
            "root": str(product.root),
        },
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    typer.echo(f"Created Product: {product.manifest.title}")
    typer.echo(f"Path: {product.root}")


@product_app.command("status")
def product_status(
    product_root: Annotated[Path, typer.Option("--product", help="Product directory.")],
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    product = ProductWorkspace(product_root.parent).load(product_root)
    payload = {
        "ok": True,
        "product": {
            "id": product.manifest.product_id,
            "title": product.manifest.title,
            "slug": product.manifest.slug,
            "createdOn": product.manifest.created_on.isoformat(),
            "root": str(product.root),
        },
        "approvals": [
            {"scope": item.scope.value, "revision": item.revision}
            for item in ApprovalLedger(product.root).list()
        ],
        "artifacts": [
            {
                "kind": item.kind.value,
                "revision": item.revision,
                "root": str(item.root),
                "integrity": "valid" if item.digest else "invalid-or-legacy",
            }
            for item in ProductWorkspace(product.root.parent).list_revisions(product)
        ],
        "publications": PublicationLedger(product.root).list(),
        "stageAttempts": [
            {
                "id": item.id,
                "runId": item.run_id,
                "stage": item.stage,
                "idempotencyKey": item.idempotency_key,
                "args": list(item.args),
                "state": item.state.value,
                "exitCode": item.exit_code,
                "output": item.output,
                "startedAt": item.started_at.isoformat(),
                "finishedAt": item.finished_at.isoformat() if item.finished_at else None,
            }
            for item in StageAttemptLedger(product.root).list()
        ],
        "runs": [
            {
                "runId": item.run_id,
                "mode": item.mode,
                "state": item.state.value,
                "commands": list(item.commands),
                "results": list(item.results),
                "startedAt": item.started_at.isoformat(),
                "finishedAt": item.finished_at.isoformat() if item.finished_at else None,
            }
            for item in StageAttemptLedger(product.root).list_runs()
        ],
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Product: {product.manifest.title}")
    typer.echo(f"Slug: {product.manifest.slug}")
    typer.echo(f"Path: {product.root}")


if __name__ == "__main__":
    app()
