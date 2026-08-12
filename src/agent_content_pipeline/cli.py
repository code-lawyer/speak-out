from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from .config import LocalConfig
from .diagnostics import SystemDoctor
from .pipeline import (
    ArticlePublicationBundle,
    ArticlePublicationWorkflow,
    PipelinePlanner,
    PipelinePlanningError,
    PipelinePlanRequest,
    article_publication_content_digest,
    article_publication_approval_key,
    load_article_publication_bundle,
    social_publication_content_digest,
    social_publication_approval_key,
)
from .publishing.article import (
    ArticleValidationError,
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
)
from .state import (
    ApprovalLedger,
    ApprovalScope,
    PublicationLedger,
    StageAttemptLedger,
    StageState,
)
from .validation import (
    validate_article_documents,
    validate_png_cover,
    validate_release_links,
    validate_release_markers,
)
from .wechat import WeChatArticleRenderer, WeChatRenderError
from .video.spec import VideoScriptSpec
from .video.cache import ProjectMediaCache
from .video.renderer import FfmpegExplainerRenderer
from .video.workflow import VideoRenderRequest, VideoRenderWorkflow, VideoWorkflowError
from .browser.cdp import CdpWebSocketClient, ChromePageController
from .browser.chrome import LocalChromeCdpDriver
from .social.browser_publishers import CONTRACTS, create_visible_chrome_publisher
from .social.models import (
    SocialCopyBundle,
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
)
from .social.workflow import (
    SocialPublicationRequest,
    SocialPublicationWorkflow,
    SocialPublicationWorkflowError,
)
from .workspace import (
    ArtifactKind,
    ArtifactIntegrityError,
    ArtifactRevision,
    ArtifactRevisionRequest,
    ArtifactSnapshot,
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


def _verified_snapshot(
    workspace: ProductWorkspace,
    product: Product,
    kind: ArtifactKind,
    revision: str,
) -> ArtifactSnapshot:
    try:
        return workspace.read_verified_revision(product, kind, revision)
    except ArtifactIntegrityError as error:
        raise typer.BadParameter(str(error), param_hint="--revision") from error


def _verified_article_bundle(
    workspace: ProductWorkspace,
    product: Product,
    article_revision: str,
    cover_revision: str,
) -> ArticlePublicationBundle:
    try:
        return load_article_publication_bundle(
            workspace,
            product,
            article_revision,
            cover_revision,
        )
    except (ArtifactIntegrityError, ArticleValidationError) as error:
        raise typer.BadParameter(str(error), param_hint="--article-revision") from error


app = typer.Typer(
    name="speak-out",
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
media_cache_app = typer.Typer(help="Inspect and seed the project-local media cache.", no_args_is_help=True)
app.add_typer(product_app, name="product")
app.add_typer(config_app, name="config")
app.add_typer(approval_app, name="approval")
app.add_typer(artifact_app, name="artifact")
app.add_typer(article_app, name="article")
app.add_typer(video_app, name="video")
app.add_typer(social_app, name="social")
app.add_typer(media_cache_app, name="media-cache")


@media_cache_app.command("status")
def media_cache_status(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    status = ProjectMediaCache(
        project_root.resolve() / ".local" / "media-cache"
    ).status()
    payload = {
        "ok": True,
        "root": str(status.root),
        "files": status.files,
        "bytes": status.bytes,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Media cache: {status.files} files, {status.bytes} bytes at {status.root}")


@media_cache_app.command("import-workspace")
def import_workspace_media_cache(
    project_root: Annotated[Path, typer.Option(help="Project repository root.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    project_root = project_root.resolve()
    result = ProjectMediaCache(
        project_root / ".local" / "media-cache"
    ).import_workspace(project_root / "workspace")
    payload = {"ok": result.conflicts == 0, **result.model_dump()}
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(
            "Media cache import: "
            f"{result.imported} imported, {result.reused} reused, "
            f"{result.conflicts} conflicts"
        )
    if result.conflicts:
        raise typer.Exit(code=1)


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
    bundle = _verified_article_bundle(
        workspace,
        product,
        article_revision,
        cover_revision,
    )

    website = LocalConfig(project_root).load().website_wechat
    website_token = website.bearer_token.get_secret_value()
    publisher = FixedIpVpsPublisher(
        endpoint=website.endpoint,
        bearer_token=website_token,
        timeout_seconds=website.request_timeout_seconds,
    )
    preview = publisher.preview(
        bundle.specification(
            source_slug=product.manifest.slug,
            target_slug=product.manifest.slug,
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
    if bundle.wechat_layout_profile is not None:
        payload["wechatLayoutProfile"] = bundle.wechat_layout_profile
        payload["wechatPreviewPath"] = str(
            workspace.resolve_artifact_file(
                product,
                ArtifactKind.ARTICLE,
                article_revision,
                "index.html",
            )
        )
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    typer.echo(f"Article ready for site and WeChat draft: {preview.target_slug}")
    if bundle.wechat_layout_profile is not None:
        typer.echo(
            "Review the exact WeChat layout before approval: "
            + payload["wechatPreviewPath"]
        )
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
    bundle = _verified_article_bundle(
        workspace,
        product,
        article_revision,
        cover_revision,
    )
    approvals = ApprovalLedger(product.root)
    missing_artifact_approvals = []
    if not approvals.has(ApprovalScope.ARTICLE, article_revision, bundle.article_digest):
        missing_artifact_approvals.append(f"article:{article_revision}")
    if not approvals.has(ApprovalScope.COVER, cover_revision, bundle.cover_digest):
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
        bundle.article_digest,
        bundle.cover_digest,
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

    bundle = _verified_article_bundle(
        workspace,
        product,
        article_revision,
        cover_revision,
    )
    spec = bundle.specification(
        source_slug=product.manifest.slug,
        target_slug=destination_slug,
    )
    website = LocalConfig(project_root).load().website_wechat
    website_token = website.bearer_token.get_secret_value()
    publisher = FixedIpVpsPublisher(
        endpoint=website.endpoint,
        bearer_token=website_token,
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
        bundle.article_digest,
        bundle.cover_digest,
        destination_slug,
        True,
    )
    if not execute:
        approvals = ApprovalLedger(product.root)
        missing = [
            f"{scope.value}:{revision}"
            for scope, revision, digest in (
                (ApprovalScope.ARTICLE, article_revision, bundle.article_digest),
                (ApprovalScope.COVER, cover_revision, bundle.cover_digest),
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
    if not exact_approvals.has(
        ApprovalScope.ARTICLE,
        article_revision,
        bundle.article_digest,
    ):
        raise typer.BadParameter(f"exact article approval is missing: {article_revision}")
    if not exact_approvals.has(
        ApprovalScope.COVER,
        cover_revision,
        bundle.cover_digest,
    ):
        raise typer.BadParameter(f"exact cover approval is missing: {cover_revision}")
    if not exact_approvals.has(
        ApprovalScope.ARTICLE_PUBLICATION,
        key,
        publication_digest,
    ):
        raise typer.BadParameter("exact article publication approval is missing")
    result = ArticlePublicationWorkflow(
        ApprovalLedger(product.root),
        publisher,
        workspace,
        product,
    ).publish(
        article_revision=article_revision,
        cover_revision=cover_revision,
        target_slug=destination_slug,
        push_to_wechat=True,
    )
    channels = interpret_article_result(result)
    log_path = write_article_publish_log(
        product.root,
        preview,
        result,
        channels,
        secret_values=(website_token,),
    )
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
        "response": redact_publication_data(
            result.response,
            secret_values=(website_token,),
        ),
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
            typer.echo("  speak-out " + " ".join(stage.args))


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
    material_count: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=36,
            help="Number of distinct Pexels clips to acquire; remote materials only.",
        ),
    ] = None,
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
    workspace = ProductWorkspace(product_root.parent)
    product = workspace.load(product_root)
    try:
        commands = PipelinePlanner(workspace=workspace, product=product).plan(
            PipelinePlanRequest(
                project_root=project_root,
                stages=tuple(stages),
                article_revision=article_revision,
                cover_revision=cover_revision,
                target_slug=target_slug,
                allow_duplicate_site=allow_duplicate_site,
                script_revision=script_revision,
                material_revision=material_revision,
                material_count=material_count,
                bgm_directory=bgm_directory,
                narration_audio=narration_audio,
                subtitles=subtitles,
                allow_edge_tts_data_transfer=allow_edge_tts_data_transfer,
                allow_pexels_data_transfer=allow_pexels_data_transfer,
                video_revision=video_revision,
                copy_revision=copy_revision,
            )
        )
    except PipelinePlanningError as error:
        raise typer.BadParameter(str(error), param_hint="--stage") from error
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
    body_html: Annotated[
        Path | None,
        typer.Option(help="Optional canonical WeChat body HTML for verification."),
    ] = None,
    wechat_html: Annotated[
        Path | None,
        typer.Option(help="Optional canonical WeChat preview HTML for verification."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    mdx_bytes = mdx.read_bytes()
    mdx_text = mdx_bytes.decode("utf-8")
    marker_issues = validate_release_markers(mdx_text)
    if marker_issues:
        raise typer.BadParameter("; ".join(marker_issues), param_hint="--mdx")
    link_issues = validate_release_links(mdx_text)
    if link_issues:
        raise typer.BadParameter("; ".join(link_issues), param_hint="--mdx")
    try:
        rendered = WeChatArticleRenderer().render(mdx_text)
    except (ValueError, WeChatRenderError) as error:
        raise typer.BadParameter(str(error), param_hint="--mdx") from error
    if (body_html is None) != (wechat_html is None):
        raise typer.BadParameter(
            "--body-html and --wechat-html must be supplied together",
            param_hint="--body-html",
        )
    if body_html is not None and wechat_html is not None:
        body_text = body_html.read_text(encoding="utf-8")
        wechat_text = wechat_html.read_text(encoding="utf-8")
        if (
            body_text != rendered.body_html
            or wechat_text != rendered.preview_html
        ):
            raise typer.BadParameter(
                "manual WeChat HTML does not match the deterministic renderer; "
                "omit --body-html and --wechat-html to generate it",
                param_hint="--body-html",
            )
    else:
        body_text = rendered.body_html
        wechat_text = rendered.preview_html
    body_bytes = body_text.encode("utf-8")
    wechat_bytes = wechat_text.encode("utf-8")
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
                "wechat-layout.json": (
                    json.dumps(
                        {"schemaVersion": 1, "profile": rendered.profile},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8"),
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
    material_count: Annotated[
        int | None,
        typer.Option(
            min=1,
            max=36,
            help="Number of distinct Pexels clips to acquire; remote materials only.",
        ),
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
    settings = LocalConfig(project_root).load()
    try:
        result = VideoRenderWorkflow(
            workspace=workspace,
            product=product,
            settings=settings,
            media_cache_root=project_root.resolve() / ".local" / "media-cache",
        ).render(
            VideoRenderRequest(
                script_revision=script_revision,
                material_revision=material_revision,
                material_count=material_count,
                bgm_directory=bgm_directory,
                narration_audio=narration_audio,
                subtitles=subtitles,
                allow_edge_tts_data_transfer=allow_edge_tts_data_transfer,
                allow_pexels_data_transfer=allow_pexels_data_transfer,
            )
        )
    except (ArtifactIntegrityError, VideoWorkflowError, ValidationError) as error:
        raise typer.BadParameter(str(error)) from error
    revision = result.revision
    rendered = result.rendered
    final_path = result.final_path
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
    copy_artifact = _verified_snapshot(
        workspace,
        product,
        ArtifactKind.SOCIAL_COPY,
        copy_revision,
    )
    video_path = video.root / "output" / "final.mp4"
    if not video_path.is_file():
        raise typer.BadParameter(f"video revision is missing final.mp4: {video_revision}")
    try:
        preview_copy_json = copy_artifact.files["copy.json"].decode("utf-8")
    except (KeyError, UnicodeError) as error:
        raise typer.BadParameter(
            "verified social-copy revision is missing UTF-8 copy.json",
            param_hint="--copy-revision",
        ) from error
    bundle = SocialCopyBundle.model_validate_json(preview_copy_json)
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
    copy_artifact = _verified_snapshot(
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
    driver = None
    if execute:
        settings = LocalConfig(project_root).load()
        configured_chrome = (
            Path(settings.browser.chrome_path) if settings.browser.chrome_path else None
        )
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
    try:
        outcome = SocialPublicationWorkflow(
            workspace=workspace,
            product=product,
            driver=driver,
            cdp_factory=CdpWebSocketClient if execute else None,
            page_attach=ChromePageController.attach if execute else None,
            publisher_factory=create_visible_chrome_publisher,
            warning_sink=lambda warning: typer.echo(f"WARNING: {warning}", err=True),
        ).publish(
            SocialPublicationRequest(
                platform=platform,
                video_revision=video_revision,
                copy_revision=copy_revision,
                execute=execute,
            )
        )
    except (ArtifactIntegrityError, SocialPublicationWorkflowError, ValidationError) as error:
        raise typer.BadParameter(str(error)) from error

    if outcome.mode == "dry-run":
        payload = {
            "ok": True,
            "mode": outcome.mode,
            "state": outcome.state,
            "platform": platform.value,
            "video": str(outcome.video_path),
            "title": outcome.title,
            "body": outcome.body,
            "tags": outcome.tags,
            "category": outcome.category,
        }
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=False))
        else:
            typer.echo(f"{platform.value}: planned")
            typer.echo("Chrome was not opened. Add --execute only after review.")
        return

    payload = {
        "ok": outcome.state == SocialPublicationState.SUBMITTED.value,
        "platform": platform.value,
        "state": outcome.state,
        "uploadState": outcome.upload_state.value,
        "snapshotRetained": outcome.snapshot_retained,
        "message": outcome.message,
        "permalink": outcome.permalink,
        "logFile": (
            str(outcome.log_path.relative_to(product.root)) if outcome.log_path else None
        ),
        "screenshotFile": (
            str(outcome.screenshot_path.relative_to(product.root))
            if outcome.screenshot_path
            else None
        ),
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"{platform.value}: {outcome.state} — {outcome.message}")
        typer.echo(
            f"upload={outcome.upload_state.value}, "
            f"snapshot-retained={'yes' if outcome.snapshot_retained else 'no'}"
        )
        if outcome.log_path:
            typer.echo(f"Log: {outcome.log_path}")
    if outcome.state != SocialPublicationState.SUBMITTED.value:
        raise typer.Exit(
            code=3 if outcome.state == SocialPublicationState.WAITING_FOR_USER.value else 1
        )


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
