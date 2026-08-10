import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline import cli
from agent_content_pipeline.cli import app
from agent_content_pipeline.pipeline import article_publication_approval_key
from agent_content_pipeline.publishing.article import ArticlePublishResult, PublicationState
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_article_preview_loads_exact_revisions_and_never_prints_the_secret(tmp_path):
    local_dir = tmp_path / ".local"
    local_dir.mkdir()
    (local_dir / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://hillward.top/api/articles"
bearer_token = "preview-secret"
request_timeout_seconds = 30
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="预览测试",
            slug="preview-test",
            created_on=date(2026, 8, 10),
        )
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={
                "article.mdx": b"---\ntitle: test\n---\nbody\n",
                "body.html": b"<p>body</p>",
                "index.html": b"<html><body>body</body></html>",
            },
        ),
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": b"png"},
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "article",
            "preview",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--article-revision",
            "v001",
            "--cover-revision",
            "v001",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "ok": True,
        "mode": "dry-run",
        "endpoint": "https://hillward.top/api/articles",
        "sourceSlug": "preview-test",
        "slug": "preview-test",
        "site": "ready",
        "wechat": "ready",
        "cover": "ready",
        "duplicateSite": False,
    }
    assert "preview-secret" not in result.stdout


def test_article_publication_approval_is_bound_to_all_exact_inputs(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="发布确认测试",
            slug="approval-test",
            created_on=date(2026, 8, 10),
        )
    )

    result = CliRunner().invoke(
        app,
        [
            "article",
            "approve-publication",
            "--product",
            str(product.root),
            "--article-revision",
            "v002",
            "--cover-revision",
            "v003",
            "--target-slug",
            "approval-test-retry",
            "--allow-duplicate-site",
            "--confirmed-by-user",
            "--json",
        ],
    )

    assert result.exit_code == 0
    expected = article_publication_approval_key(
        "v002", "v003", "approval-test-retry", True
    )
    assert json.loads(result.stdout)["approval"]["revision"] == expected
    assert ApprovalLedger(product.root).has(ApprovalScope.ARTICLE_PUBLICATION, expected)


def test_article_publish_reports_channels_and_writes_a_secret_safe_log(tmp_path, monkeypatch):
    local_dir = tmp_path / ".local"
    local_dir.mkdir()
    (local_dir / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://hillward.top/api/articles"
bearer_token = "publish-secret"
request_timeout_seconds = 30
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="发布测试",
            slug="publish-test",
            created_on=date(2026, 8, 10),
        )
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={
                "article.mdx": b"---\ntitle: test\n---\nbody\n",
                "body.html": b"<p>body</p>",
                "index.html": b"<html><body>body</body></html>",
            },
        ),
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": b"private-cover-bytes"},
        ),
    )
    ledger = ApprovalLedger(product.root)
    ledger.record(ApprovalScope.ARTICLE, "v001")
    ledger.record(ApprovalScope.COVER, "v001")
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "publish-test", True),
    )

    def succeed(self, preview):
        assert preview.request_body["coverImageBase64"].startswith("data:image/png;base64,")
        return ArticlePublishResult(
            state=PublicationState.SUCCEEDED,
            http_status=200,
            response={
                "success": True,
                "wechatPushed": True,
                "url": "/articles/publish-test",
                "accessToken": "remote-secret-token",
            },
        )

    monkeypatch.setattr(cli.FixedIpVpsPublisher, "publish", succeed)
    result = CliRunner().invoke(
        app,
        [
            "article",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--article-revision",
            "v001",
            "--cover-revision",
            "v001",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["site"] == "published"
    assert payload["cover"] == "uploaded"
    assert payload["wechat"] == "drafted"
    log_text = (product.root / payload["logFile"]).read_text(encoding="utf-8")
    assert "publish-secret" not in result.stdout + log_text
    assert "private-cover-bytes" not in result.stdout + log_text
    assert "remote-secret-token" not in result.stdout + log_text
    assert payload["response"]["accessToken"] == "[REDACTED]"
    assert payload["state"] == "succeeded"
