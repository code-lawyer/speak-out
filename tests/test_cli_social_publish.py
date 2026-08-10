import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from agent_content_pipeline import cli
from agent_content_pipeline.browser.cdp import CdpError
from agent_content_pipeline.cli import app
from agent_content_pipeline.pipeline import (
    social_publication_approval_key,
    social_publication_content_digest,
)
from agent_content_pipeline.social.models import (
    SocialPlatform,
    SocialPublicationState,
    SocialPublishResult,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope, PublicationLedger
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_social_publish_uses_exact_approvals_and_records_independent_result(tmp_path, monkeypatch):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "unused"

[browser]
chrome_path = "C:/fake/chrome.exe"
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="社交发布测试",
            slug="social-publish-test",
            created_on=date(2026, 8, 10),
        )
    )
    staging = product.root / "video" / "work" / "done"
    (staging / "output").mkdir(parents=True)
    (staging / "output" / "final.mp4").write_bytes(b"video")
    video_revision = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        staging,
    )
    copy_revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.SOCIAL_COPY,
            files={
                "copy.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "platforms": {
                            "xiaohongshu": {"title": "小红书", "body": "正文", "tags": ["AI"]},
                            "douyin": {"title": "抖音", "body": "正文", "tags": ["AI"]},
                            "bilibili": {
                                "title": "B站",
                                "body": "简介",
                                "tags": ["AI"],
                                "category": "知识",
                            },
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            },
        ),
    )
    approvals = ApprovalLedger(product.root)
    approvals.record(ApprovalScope.VIDEO, "v001", video_revision.digest)

    preview = CliRunner().invoke(
        app,
        [
            "social",
            "preview",
            "--product",
            str(product.root),
            "--platform",
            "bilibili",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["mode"] == "dry-run"
    assert preview_payload["target"] == "immediate-publication"
    assert preview_payload["title"] == "B站"
    assert preview_payload["videoApproved"] is True
    assert preview_payload["publicationApproved"] is False

    approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        social_publication_approval_key("v001", "v001", SocialPlatform.BILIBILI),
        social_publication_content_digest(
            video_revision.digest,
            copy_revision.digest,
            SocialPlatform.BILIBILI,
        ),
    )
    approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        social_publication_approval_key("v001", "v001", SocialPlatform.DOUYIN),
        social_publication_content_digest(
            video_revision.digest,
            copy_revision.digest,
            SocialPlatform.DOUYIN,
        ),
    )
    approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        social_publication_approval_key("v001", "v001", SocialPlatform.XIAOHONGSHU),
        social_publication_content_digest(
            video_revision.digest,
            copy_revision.digest,
            SocialPlatform.XIAOHONGSHU,
        ),
    )

    class FakeSession:
        def __init__(self, platform):
            self.platform = platform
            self.websocket_url = f"ws://fake/{platform}"
            self.process_id = 123

    driver_calls = []
    stopped_sessions = []

    class FakeDriver:
        def __init__(self, *, project_root, chrome_path):
            assert project_root == tmp_path
            assert chrome_path == Path("C:/fake/chrome.exe")

        def launch(self, *, platform, start_url):
            driver_calls.append((platform, start_url))
            assert platform in {"bilibili", "douyin", "xiaohongshu"}
            assert start_url.startswith("https://")
            return FakeSession(platform)

        def stop_launched_session(self, session):
            stopped_sessions.append(session.platform)

    class FakeCdp:
        def __init__(self, url):
            assert url.startswith("ws://fake/")
            if url.endswith("/xiaohongshu"):
                raise CdpError("CDP connection failed")

        def close(self):
            pass

    class FakePublisher:
        def __init__(self, platform):
            self.platform = platform
            self.contract = type("Contract", (), {"upload_url": "https://example.com/upload"})()

        def publish(self, page, spec):
            if self.platform == SocialPlatform.DOUYIN:
                raise CdpError("connection lost after upload")
            assert spec.video_path.read_bytes() == b"video"
            assert spec.category == "知识"
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.SUBMITTED,
                message="accepted",
            )

    monkeypatch.setattr(cli, "LocalChromeCdpDriver", FakeDriver)
    monkeypatch.setattr(cli, "CdpWebSocketClient", FakeCdp)
    class FakePage:
        def screenshot(self, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"diagnostic")
            return path

    monkeypatch.setattr(cli.ChromePageController, "attach", lambda cdp: FakePage())
    monkeypatch.setattr(cli, "VisibleChromePlatformPublisher", FakePublisher)

    dry_run = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "bilibili",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--json",
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.stdout)["mode"] == "dry-run"
    assert json.loads(dry_run.stdout)["state"] == "planned"
    assert driver_calls == []

    bilibili_key = social_publication_approval_key(
        "v001",
        "v001",
        SocialPlatform.BILIBILI,
    )
    PublicationLedger(product.root).record_state(
        "social:bilibili",
        bilibili_key,
        "running",
    )
    contended = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "bilibili",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--execute",
            "--json",
        ],
    )
    assert contended.exit_code != 0
    assert "running" in contended.output
    assert driver_calls == []
    PublicationLedger(product.root).record_state(
        "social:bilibili",
        bilibili_key,
        "failed",
    )

    original_has = ApprovalLedger.has
    approval_checks = 0

    def interrupt_after_private_copy(self, scope, revision, content_digest=None):
        nonlocal approval_checks
        approval_checks += 1
        if approval_checks == 3:
            raise KeyboardInterrupt()
        return original_has(self, scope, revision, content_digest)

    monkeypatch.setattr(ApprovalLedger, "has", interrupt_after_private_copy)
    interrupted = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "bilibili",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--execute",
            "--json",
        ],
    )
    assert interrupted.exit_code == 130
    assert not list((product.root / "publish" / ".staging").glob("*.mp4"))
    monkeypatch.setattr(ApprovalLedger, "has", original_has)

    result = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "bilibili",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--execute",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["platform"] == "bilibili"
    assert payload["state"] == "submitted"
    assert (product.root / payload["logFile"]).is_file()
    assert len(driver_calls) == 1
    assert not list((product.root / "publish" / ".staging").glob("*.mp4"))
    status = CliRunner().invoke(
        app,
        ["product", "status", "--product", str(product.root), "--json"],
    )
    publications = json.loads(status.stdout)["publications"]
    assert publications[0]["destination"] == "social:bilibili"
    assert publications[0]["state"] == "submitted"

    cdp_failed = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "xiaohongshu",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--execute",
            "--json",
        ],
    )
    assert cdp_failed.exit_code != 0
    assert stopped_sessions == ["xiaohongshu"]
    assert not list((product.root / "publish" / ".staging").glob("*.mp4"))

    uncertain = CliRunner().invoke(
        app,
        [
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "douyin",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--execute",
            "--json",
        ],
    )
    assert uncertain.exit_code != 0
    assert uncertain.stdout, repr(uncertain.exception)
    uncertain_payload = json.loads(uncertain.stdout)
    assert uncertain_payload["state"] == "unknown"
    assert (product.root / uncertain_payload["screenshotFile"]).read_bytes() == b"diagnostic"
    assert PublicationLedger(product.root).get_state(
        "social:douyin",
        social_publication_approval_key("v001", "v001", SocialPlatform.DOUYIN),
    ) == "unknown"


def test_social_close_only_closes_the_platform_dedicated_profile(tmp_path, monkeypatch):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "unused"

[browser]
chrome_path = "C:/fake/chrome.exe"
""".lstrip(),
        encoding="utf-8",
    )
    closed: list[str] = []

    class FakeSession:
        platform = "bilibili"
        profile_root = tmp_path / ".local" / "browser-profiles" / "bilibili"

    class FakeDriver:
        def __init__(self, *, project_root, chrome_path):
            assert project_root == tmp_path
            assert chrome_path == Path("C:/fake/chrome.exe")

        def connect_existing(self, *, platform):
            assert platform == "bilibili"
            return FakeSession()

        def close(self, session):
            closed.append(session.platform)

    monkeypatch.setattr(cli, "LocalChromeCdpDriver", FakeDriver)

    result = CliRunner().invoke(
        app,
        [
            "social",
            "close",
            "--project-root",
            str(tmp_path),
            "--platform",
            "bilibili",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["browser"] == "closed"
    assert closed == ["bilibili"]


def test_private_snapshot_cleanup_retries_and_records_persistent_windows_lock(
    tmp_path,
    monkeypatch,
):
    staging = tmp_path / "publish" / ".staging"
    staging.mkdir(parents=True)
    snapshot = staging / "private.mp4"
    snapshot.write_bytes(b"video")
    original_unlink = Path.unlink
    attempts = 0

    def transient_lock(path, *args, **kwargs):
        nonlocal attempts
        if path == snapshot and attempts < 2:
            attempts += 1
            raise PermissionError("file is still held by Chrome")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", transient_lock)
    assert cli._remove_private_snapshot(snapshot, delay_seconds=0) is None
    assert attempts == 2
    assert snapshot.exists() is False

    snapshot.write_bytes(b"video")

    def persistent_lock(path, *args, **kwargs):
        if path == snapshot:
            raise PermissionError("file is still held by Chrome")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", persistent_lock)
    warning = cli._remove_private_snapshot(snapshot, attempts=2, delay_seconds=0)
    marker = snapshot.with_suffix(snapshot.suffix + ".cleanup-pending")
    assert warning is not None
    assert marker.is_file()

    monkeypatch.setattr(Path, "unlink", original_unlink)
    cli._sweep_private_snapshots(staging, delay_seconds=0)
    assert snapshot.exists() is False
    assert marker.exists() is False
