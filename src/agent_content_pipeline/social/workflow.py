from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ..pipeline import social_publication_approval_key, social_publication_content_digest
from ..state import (
    ApprovalLedger,
    ApprovalScope,
    PublicationLedger,
    PublicationRecordState,
)
from ..workspace import ArtifactKind, Product, ProductWorkspace
from .browser_publishers import CONTRACTS, VisibleChromePlatformPublisher
from .models import (
    SocialCopyBundle,
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialPublishResult,
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
        self._publisher_factory = publisher_factory or VisibleChromePlatformPublisher
        self._approvals = approvals or ApprovalLedger(product.root)
        self._publications = publications or PublicationLedger(product.root)
        self._warning_sink = warning_sink or (lambda _warning: None)

    def publish(self, request: SocialPublicationRequest) -> SocialPublicationOutcome:
        video = self._workspace.verify_revision(
            self._product,
            ArtifactKind.VIDEO_RENDER,
            request.video_revision,
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
        source_video = video.root / "output" / "final.mp4"
        if not source_video.is_file():
            raise SocialPublicationWorkflowError(
                f"approved video file is missing: {source_video}"
            )
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
        private_path = staging_root / f"{uuid4()}-{request.platform.value}.mp4"
        video_copy = self._workspace.copy_verified_file(
            self._product,
            ArtifactKind.VIDEO_RENDER,
            request.video_revision,
            "output/final.mp4",
            private_path,
        )
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
            )
            log_path = self._write_log(
                request,
                destination,
                key,
                result,
                timestamp,
                screenshot,
            )
            return self._outcome(
                "execute",
                result.state.value,
                spec,
                message=result.message,
                permalink=result.permalink,
                log_path=log_path,
                screenshot_path=screenshot,
            )
        finally:
            warning = self._remove_private_snapshot(video_copy.path)
            if warning is not None:
                self._warning_sink(warning)

    def _execute_browser_attempt(
        self,
        request: SocialPublicationRequest,
        spec: SocialPostSpec,
        destination: str,
        key: str,
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
                    attempt.mark_external_started()
                    try:
                        result = self._publisher_factory(request.platform).publish(page, spec)
                    except Exception as error:
                        result = SocialPublishResult(
                            platform=request.platform,
                            state=SocialPublicationState.UNKNOWN,
                            message=(
                                "browser automation was interrupted after opening the creator "
                                "flow; reconcile before retrying "
                                f"({type(error).__name__})"
                            ),
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
        )

    @staticmethod
    def _remove_private_snapshot(
        path: Path,
        *,
        attempts: int = 5,
        delay_seconds: float = 0.2,
    ) -> str | None:
        marker = path.with_suffix(path.suffix + ".cleanup-pending")
        last_error: OSError | None = None
        for attempt in range(max(1, attempts)):
            try:
                path.unlink(missing_ok=True)
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
