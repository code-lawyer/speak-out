import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline import cli
from agent_content_pipeline.cli import app
from agent_content_pipeline.pipeline import (
    article_publication_approval_key,
    article_publication_content_digest,
)
from agent_content_pipeline.publishing.article import ArticlePublishResult, PublicationState
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def png_fixture(width: int = 900, height: int = 383) -> bytes:
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            (13).to_bytes(4, "big"),
            b"IHDR",
            width.to_bytes(4, "big"),
            height.to_bytes(4, "big"),
            b"\x08\x02\x00\x00\x00",
            b"\x00\x00\x00\x00",
            (1).to_bytes(4, "big"),
            b"IDAT",
            b"\x00",
            b"\x00\x00\x00\x00",
            (0).to_bytes(4, "big"),
            b"IEND",
            b"\x00\x00\x00\x00",
        )
    )


def valid_article_artifact_files(title: str) -> dict[str, bytes]:
    markdown = f'''---
title: "{title}"
date: 2026-08-10
category: essay
tags: ["AI", "思考"]
summary: "测试摘要"
---

正文
'''
    style = "font-size: 16px; color: #222; line-height: 1.8; text-align: left"
    body = (
        f'<p style="{style}">{title}</p>'
        f'<p style="{style}">2026.08.10</p>'
        f'<p style="{style}">正文</p>'
    )
    return {
        "article.mdx": markdown.encode("utf-8"),
        "body.html": body.encode("utf-8"),
        "index.html": f"<html><body>{body}</body></html>".encode("utf-8"),
    }


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
    article = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files=valid_article_artifact_files("斩我斋：预览测试"),
        ),
    )
    cover = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": png_fixture()},
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


def test_article_preview_revalidates_hash_valid_legacy_artifacts(tmp_path):
    local_dir = tmp_path / ".local"
    local_dir.mkdir()
    (local_dir / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://hillward.top/api/articles"
bearer_token = "preview-secret"
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="旧产物校验",
            slug="legacy-validation-test",
            created_on=date(2026, 8, 10),
        )
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={
                "article.mdx": b"---\ntitle: legacy\n---\nbody\n",
                "body.html": b"<p>body</p><img src='inline.png'>",
                "index.html": b"<html><body><img src='inline.png'></body></html>",
            },
        ),
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": b"not-a-png"},
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
        ],
    )

    assert result.exit_code != 0
    assert "title must start" in result.output
    assert "article body must not contain images" in result.output
    assert "not a valid PNG" in result.output


def test_article_publication_approval_is_bound_to_all_exact_inputs(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="发布确认测试",
            slug="approval-test",
            created_on=date(2026, 8, 10),
        )
    )
    article = None
    for _ in range(2):
        article = workspace.add_revision(
                product,
                ArtifactRevisionRequest(
                    kind=ArtifactKind.ARTICLE,
                    files=valid_article_artifact_files("斩我斋：批准测试"),
            ),
        )
    cover = None
    for _ in range(3):
        cover = workspace.add_revision(
            product,
                ArtifactRevisionRequest(
                    kind=ArtifactKind.COVER,
                    files={"cover.png": png_fixture()},
            ),
        )
    assert article is not None and cover is not None
    approvals = ApprovalLedger(product.root)
    approvals.record(ApprovalScope.ARTICLE, "v002", article.digest)
    approvals.record(ApprovalScope.COVER, "v003", cover.digest)

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
    article = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files=valid_article_artifact_files("斩我斋：发布测试"),
        ),
    )
    cover = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": png_fixture()},
        ),
    )
    ledger = ApprovalLedger(product.root)
    ledger.record(ApprovalScope.ARTICLE, "v001", article.digest)
    ledger.record(ApprovalScope.COVER, "v001", cover.digest)
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "publish-test", True),
        article_publication_content_digest(
            article.digest,
            cover.digest,
            "publish-test",
            True,
        ),
    )

    publish_calls = []

    encoded_cover = None

    def succeed(self, preview):
        nonlocal encoded_cover
        publish_calls.append(preview)
        assert preview.request_body["coverImageBase64"].startswith("data:image/png;base64,")
        encoded_cover = preview.request_body["coverImageBase64"]
        return ArticlePublishResult(
            state=PublicationState.SUCCEEDED,
            http_status=200,
            response={
                "success": True,
                "wechatPushed": True,
                "url": "/articles/publish-test",
                "message": "accepted with publish-secret",
                "accessToken": "remote-secret-token",
                "setCookie": "private-session-cookie",
            },
        )

    monkeypatch.setattr(cli.FixedIpVpsPublisher, "publish", succeed)
    dry_run = CliRunner().invoke(
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
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.stdout)["mode"] == "dry-run"
    assert json.loads(dry_run.stdout)["state"] == "planned"
    assert publish_calls == []

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
            "--execute",
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
    assert encoded_cover not in result.stdout + log_text
    assert "remote-secret-token" not in result.stdout + log_text
    assert "private-session-cookie" not in result.stdout + log_text
    assert payload["response"]["accessToken"] == "[REDACTED]"
    assert payload["response"]["setCookie"] == "[REDACTED]"
    assert payload["state"] == "succeeded"
    assert len(publish_calls) == 1
