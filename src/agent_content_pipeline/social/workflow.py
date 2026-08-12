from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict

from ..pipeline import social_publication_approval_key, social_publication_content_digest
from ..state import (
    ApprovalLedger,
    ApprovalScope,
    PublicationLedger,
    PublicationRecordState,
)
from ..workspace import (
    ArtifactFileIdentity,
    ArtifactKind,
    Product,
    ProductWorkspace,
    VerifiedFileCopy,
)
from .browser_publishers import (
    BrowserPublishLifecycle,
    CONTRACTS,
    create_visible_chrome_publisher,
)
from .models import (
    SocialCopyBundle,
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialPublishResult,
    SocialUploadState,
)


class BrowserDriver(Protocol):
    def launch(self, *, platform: str, start_url: str) -> Any: ...

    def stop_launched_session(self, session: Any) -> None: ...


class SocialPublicationWorkflowError(ValueError):
    pass


class SocialPublicationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: SocialPlatform
    video_revision: str
    copy_revision: str
    execute: bool = False


class SocialPublicationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    mode: str
    platform: SocialPlatform
    state: str
    message: str | None = None
    permalink: str | None = None
    video_path: Path
    title: str
    body: str
    tags: list[str]
    category: str | None = None
    log_path: Path | None = None
    screenshot_path: Path | None = None
    upload_state: SocialUploadState = SocialUploadState.NOT_STARTED
    snapshot_retained: bool = False


class SocialPublicationWorkflow:
    """Publish one approved social payload while owning all temporary resources."""

    def __init__(
        self,
        *,
        workspace: ProductWorkspace,
        product: Product,
        driver: BrowserDriver | None = None,
        cdp_factory: Callable[[str], Any] | None = None,
        page_attach: Callable[[Any], Any] | None = None,
        publisher_factory: Callable[[SocialPlatform], Any] | None = None,
        approvals: ApprovalLedger | None = None,
        publications: PublicationLedger | None = None,
        warning_sink: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._product = product
        self._driver = driver
        self._cdp_factory = cdp_factory
        self._page_attach = page_attach
        self._publisher_factory = publisher_factory or create_visible_chrome_publisher
        self._approvals = approvals or ApprovalLedger(product.root)
        self._publications = publications or PublicationLedger(product.root)
        self._warning_sink = warning_sink or (lambda _warning: None)

    def publish(self, request: SocialPublicationRequest) -> SocialPublicationOutcome:
        video = self._workspace.verify_revision(
            self._product,
            ArtifactKind.VIDEO_RENDER,
            request.video_revision,
        )
        video_identity = self._workspace.artifact_file_identity(
            self._product,
            ArtifactKind.VIDEO_RENDER,
            request.video_revision,
            "output/final.mp4",
        )
        copy = self._workspace.read_verified_revision(
            self._product,
            ArtifactKind.SOCIAL_COPY,
            request.copy_revision,
        )
        key = social_publication_approval_key(
            request.video_revision,
            request.copy_revision,
            request.platform,
        )
        publication_digest = social_publication_content_digest(
            video.digest,
            copy.digest,
            request.platform,
        )
        missing: list[str] = []
        if not self._approvals.has(ApprovalScope.VIDEO, request.video_revision, video.digest):
            missing.append(f"video:{request.video_revision}")
        if not self._approvals.has(
            ApprovalScope.SOCIAL_PUBLICATION,
            key,
            publication_digest,
        ):
            missing.append(f"social-publication:{key}")
        if missing:
            raise SocialPublicationWorkflowError(
                "missing explicit approval: " + ", ".join(missing)
            )

        destination = f"social:{request.platform.value}"
        prior = self._publications.get_state(destination, key)
        if prior == SocialPublicationState.SUBMITTED.value:
            raise SocialPublicationWorkflowError(
                f"publication already submitted: {destination}:{key}"
            )
        if prior in {SocialPublicationState.UNKNOWN.value, "partial", "running"}:
            raise SocialPublicationWorkflowError(
                f"prior publication state is {prior}; reconcile before retrying: "
                f"{destination}:{key}"
            )

        try:
            copy_json = copy.files["copy.json"].decode("utf-8")
        except (KeyError, UnicodeError) as error:
            raise SocialPublicationWorkflowError(
                "verified social-copy revision is missing UTF-8 copy.json"
            ) from error
        bundle = SocialCopyBundle.model_validate_json(copy_json)
        platform_copy = bundle.platforms[request.platform]
        try:
            source_video = self._workspace.resolve_artifact_file(
                self._product,
                ArtifactKind.VIDEO_RENDER,
                request.video_revision,
                "output/final.mp4",
            )
        except ValueError as error:
            raise SocialPublicationWorkflowError(
                f"approved video file is missing: {request.video_revision}"
            ) from error
        spec = SocialPostSpec(
            platform=request.platform,
            title=platform_copy.title,
            body=platform_copy.body,
            tags=platform_copy.tags,
            video_path=source_video,
            category=platform_copy.category,
        )
        if not request.execute:
            return self._outcome("dry-run", "planned", spec)
        if self._driver is None or self._cdp_factory is None or self._page_attach is None:
            raise SocialPublicationWorkflowError(
                "browser adapters are required for an executed social publication"
            )

        staging_root = self._product.root / "publish" / ".staging"
        for warning in self._sweep_private_snapshots(staging_root):
            self._warning_sink(warning)
        snapshot_id = hashlib.sha256(
            f"{destination}:{key}:{publication_digest}".encode("utf-8")
        ).hexdigest()[:24]
        private_path = staging_root / f"{snapshot_id}-{request.platform.value}.mp4"
        video_copy = self._load_or_create_private_snapshot(
            request=request,
            destination=destination,
            key=key,
            publication_digest=publication_digest,
            private_path=private_path,
            approved_video=video_identity,
        )
        lifecycle = BrowserPublishLifecycle()
        snapshot_retained = False
        try:
            exact_digest = social_publication_content_digest(
                video_copy.revision_digest,
                copy.digest,
                request.platform,
            )
            if not self._approvals.has(
                ApprovalScope.VIDEO,
                request.video_revision,
                video_copy.revision_digest,
            ) or not self._approvals.has(
                ApprovalScope.SOCIAL_PUBLICATION,
                key,
                exact_digest,
            ):
                raise SocialPublicationWorkflowError(
                    "exact social publication approval changed before upload"
                )
            private_spec = spec.model_copy(update={"video_path": video_copy.path})
            result, timestamp, screenshot = self._execute_browser_attempt(
                request,
                private_spec,
                destination,
                key,
                lifecycle,
            )
            snapshot_retained = lifecycle.upload_state == SocialUploadState.UPLOADING
            log_path = self._write_log(
                request,
                destination,
                key,
                result,
                timestamp,
                screenshot,
                snapshot_retained=snapshot_retained,
            )
            return self._outcome(
                "execute",
                result.state.value,
                spec,
                message=result.message,
                permalink=result.permalink,
                log_path=log_path,
                screenshot_path=screenshot,
                upload_state=result.upload_state,
                snapshot_retained=snapshot_retained,
            )
        finally:
            if lifecycle.upload_state == SocialUploadState.UPLOADING:
                snapshot_retained = True
            else:
                warning = self._remove_private_snapshot(video_copy.path)
                if warning is not None:
                    self._warning_sink(warning)

    def _load_or_create_private_snapshot(
        self,
        *,
        request: SocialPublicationRequest,
        destination: str,
        key: str,
        publication_digest: str,
        private_path: Path,
        approved_video: ArtifactFileIdentity,
    ) -> VerifiedFileCopy:
        metadata_path = private_path.with_suffix(".snapshot.json")
        expected_metadata = {
            "destination": destination,
            "idempotencyKey": key,
            "publicationDigest": publication_digest,
            "videoRevision": request.video_revision,
            "copyRevision": request.copy_revision,
            "platform": request.platform.value,
        }
        if private_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise SocialPublicationWorkflowError(
                    "retained upload snapshot metadata is missing or invalid; "
                    "do not replace a file that Chrome may still be reading"
                ) from error
            if any(metadata.get(name) != value for name, value in expected_metadata.items()):
                raise SocialPublicationWorkflowError(
                    "retained upload snapshot belongs to a different approved publication"
                )
            file_sha256 = self._sha256_file(private_path)
            if (
                metadata.get("fileSha256") != approved_video.file_sha256
                or file_sha256 != approved_video.file_sha256
                or private_path.stat().st_size != approved_video.bytes
            ):
                raise SocialPublicationWorkflowError(
                    "retained upload snapshot no longer matches the approved video artifact"
                )
            revision_digest = metadata.get("revisionDigest")
            if revision_digest != approved_video.revision_digest:
                raise SocialPublicationWorkflowError(
                    "retained upload snapshot no longer matches the approved video artifact"
                )
            return VerifiedFileCopy(
                path=private_path,
                revision_digest=revision_digest,
                file_sha256=file_sha256,
            )

        metadata_path.unlink(missing_ok=True)
        video_copy = self._workspace.copy_verified_file(
            self._product,
            ArtifactKind.VIDEO_RENDER,
            request.video_revision,
            "output/final.mp4",
            private_path,
        )
        if (
            video_copy.revision_digest != approved_video.revision_digest
            or video_copy.file_sha256 != approved_video.file_sha256
            or video_copy.path.stat().st_size != approved_video.bytes
        ):
            private_path.unlink(missing_ok=True)
            raise SocialPublicationWorkflowError(
                "private upload snapshot does not match the approved video artifact"
            )
        metadata = {
            **expected_metadata,
            "revisionDigest": video_copy.revision_digest,
            "fileSha256": video_copy.file_sha256,
        }
        try:
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            private_path.unlink(missing_ok=True)
            raise
        return video_copy

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _execute_browser_attempt(
        self,
        request: SocialPublicationRequest,
        spec: SocialPostSpec,
        destination: str,
        key: str,
        lifecycle: BrowserPublishLifecycle,
    ) -> tuple[SocialPublishResult, datetime, Path | None]:
        assert self._driver is not None
        assert self._cdp_factory is not None
        assert self._page_attach is not None
        contract = CONTRACTS[request.platform]
        session = None
        try:
            session = self._driver.launch(
                platform=request.platform.value,
                start_url=contract.upload_url,
            )
            cdp = self._cdp_factory(session.websocket_url)
        except BaseException:
            if session is not None and session.process_id is not None:
                try:
                    self._driver.stop_launched_session(session)
                except BaseException:
                    pass
            raise

        timestamp = datetime.now(UTC)
        page = None
        screenshot: Path | None = None
        result = SocialPublishResult(
            platform=request.platform,
            state=SocialPublicationState.UNKNOWN,
            message="browser automation did not complete; reconcile before retrying",
        )
        try:
            attempt = self._publications.begin_attempt(destination, key)
            if not attempt.claim.acquired:
                raise SocialPublicationWorkflowError(
                    "publication is already claimed with state "
                    f"{attempt.claim.prior_state}: {destination}:{key}"
                )
            with attempt:
                lifecycle.bind_submission_start(attempt.mark_external_started)
                try:
                    page = self._page_attach(cdp)
                except Exception as error:
                    result = SocialPublishResult(
                        platform=request.platform,
                        state=SocialPublicationState.FAILED,
                        message=(
                            "the local creator page could not be attached before publication; "
                            f"this attempt is safe to retry ({type(error).__name__})"
                        ),
                    )
                else:
                    try:
                        result = self._publisher_factory(request.platform).publish(
                            page,
                            spec,
                            lifecycle=lifecycle,
                        )
                    except Exception as error:
                        state = (
                            SocialPublicationState.UNKNOWN
                            if lifecycle.submission_started
                            else SocialPublicationState.FAILED
                        )
                        result = SocialPublishResult(
                            platform=request.platform,
                            state=state,
                            message=(
                                "browser automation was interrupted "
                                + (
                                    "after the final submit seam; reconcile before retrying "
                                    if lifecycle.submission_started
                                    else "before final submission; this stage is safe to retry "
                                )
                                + f"({type(error).__name__})"
                            ),
                            upload_state=lifecycle.upload_state,
                            submission_started=lifecycle.submission_started,
                        )
                attempt.finish(PublicationRecordState(result.state.value))
        finally:
            if page is not None and result.state != SocialPublicationState.SUBMITTED:
                candidate = self._product.root / "logs" / (
                    timestamp.isoformat().replace(":", "-").replace(".", "-")
                    + f"-{request.platform.value}.png"
                )
                try:
                    screenshot = page.screenshot(candidate)
                except BaseException:
                    screenshot = None
            try:
                cdp.close()
            except BaseException:
                pass
        return result, timestamp, screenshot

    def _write_log(
        self,
        request: SocialPublicationRequest,
        destination: str,
        key: str,
        result: SocialPublishResult,
        timestamp: datetime,
        screenshot: Path | None,
        *,
        snapshot_retained: bool,
    ) -> Path:
        log_root = self._product.root / "logs"
        log_root.mkdir(exist_ok=True)
        path = log_root / (
            timestamp.isoformat().replace(":", "-").replace(".", "-")
            + f"-{request.platform.value}.json"
        )
        path.write_text(
            json.dumps(
                {
                    "timestamp": timestamp.isoformat(),
                    "destination": destination,
                    "idempotencyKey": key,
                    "videoRevision": request.video_revision,
                    "copyRevision": request.copy_revision,
                    "platform": request.platform.value,
                    "state": result.state.value,
                    "uploadState": result.upload_state.value,
                    "submissionStarted": result.submission_started,
                    "snapshotRetained": snapshot_retained,
                    "message": result.message,
                    "permalink": result.permalink,
                    "screenshotFile": (
                        str(screenshot.relative_to(self._product.root)) if screenshot else None
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _outcome(
        mode: str,
        state: str,
        spec: SocialPostSpec,
        *,
        message: str | None = None,
        permalink: str | None = None,
        log_path: Path | None = None,
        screenshot_path: Path | None = None,
        upload_state: SocialUploadState = SocialUploadState.NOT_STARTED,
        snapshot_retained: bool = False,
    ) -> SocialPublicationOutcome:
        return SocialPublicationOutcome(
            mode=mode,
            platform=spec.platform,
            state=state,
            message=message,
            permalink=permalink,
            video_path=spec.video_path,
            title=spec.title,
            body=spec.body,
            tags=spec.tags,
            category=spec.category,
            log_path=log_path,
            screenshot_path=screenshot_path,
            upload_state=upload_state,
            snapshot_retained=snapshot_retained,
        )

    @staticmethod
    def _remove_private_snapshot(
        path: Path,
        *,
        attempts: int = 5,
        delay_seconds: float = 0.2,
    ) -> str | None:
        marker = path.with_suffix(path.suffix + ".cleanup-pending")
        metadata = path.with_suffix(".snapshot.json")
        last_error: OSError | None = None
        for attempt in range(max(1, attempts)):
            try:
                path.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                return None
            except OSError as error:
                last_error = error
                if attempt + 1 < max(1, attempts):
                    time.sleep(max(0, delay_seconds))
        warning = f"private upload snapshot cleanup is pending: {path} ({last_error})"
        try:
            marker.write_text(path.name + "\n", encoding="utf-8")
        except OSError as marker_error:
            warning += f"; cleanup marker could not be written ({marker_error})"
        return warning

    @classmethod
    def _sweep_private_snapshots(cls, staging_root: Path) -> tuple[str, ...]:
        warnings: list[str] = []
        for marker in sorted(staging_root.glob("*.cleanup-pending")):
            suffix = ".cleanup-pending"
            target = Path(str(marker)[: -len(suffix)])
            warning = cls._remove_private_snapshot(target)
            if warning is not None:
                warnings.append(warning)
        return tuple(warnings)
