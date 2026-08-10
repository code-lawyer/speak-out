import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from agent_content_pipeline import cli
from agent_content_pipeline.cli import app
from agent_content_pipeline.pipeline import social_publication_approval_key
from agent_content_pipeline.social.models import (
    SocialPlatform,
    SocialPublicationState,
    SocialPublishResult,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
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
    workspace.commit_revision_directory(product, ArtifactKind.VIDEO_RENDER, staging)
    workspace.add_revision(
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
    approvals.record(ApprovalScope.VIDEO, "v001")
    approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        social_publication_approval_key("v001", "v001", SocialPlatform.BILIBILI),
    )

    class FakeSession:
        websocket_url = "ws://fake"

    class FakeDriver:
        def __init__(self, *, project_root, chrome_path):
            assert project_root == tmp_path
            assert chrome_path == Path("C:/fake/chrome.exe")

        def launch(self, *, platform, start_url):
            assert platform == "bilibili"
            assert start_url.startswith("https://member.bilibili.com/")
            return FakeSession()

    class FakeCdp:
        def __init__(self, url):
            assert url == "ws://fake"

        def close(self):
            pass

    class FakePublisher:
        def __init__(self, platform):
            assert platform == SocialPlatform.BILIBILI
            self.contract = type("Contract", (), {"upload_url": "https://member.bilibili.com/upload"})()

        def publish(self, page, spec):
            assert spec.video_path.read_bytes() == b"video"
            assert spec.category == "知识"
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.SUBMITTED,
                message="accepted",
            )

    monkeypatch.setattr(cli, "LocalChromeCdpDriver", FakeDriver)
    monkeypatch.setattr(cli, "CdpWebSocketClient", FakeCdp)
    monkeypatch.setattr(cli.ChromePageController, "attach", lambda cdp: object())
    monkeypatch.setattr(cli, "VisibleChromePlatformPublisher", FakePublisher)

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
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["platform"] == "bilibili"
    assert payload["state"] == "submitted"
    assert (product.root / payload["logFile"]).is_file()
    status = CliRunner().invoke(
        app,
        ["product", "status", "--product", str(product.root), "--json"],
    )
    publications = json.loads(status.stdout)["publications"]
    assert publications[0]["destination"] == "social:bilibili"
    assert publications[0]["state"] == "submitted"
