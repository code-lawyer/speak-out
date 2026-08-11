from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event, Lock

import httpx
import pytest

from agent_content_pipeline.pipeline import (
    AlreadyPublished,
    ApprovalRequired,
    ArticlePublicationWorkflow,
    UnsafeToRetry,
    article_publication_approval_key,
    article_publication_content_digest,
    load_article_publication_bundle,
)
from agent_content_pipeline.publishing.article import (
    ArticlePublishPreview,
    ArticlePublishResult,
    FixedIpVpsPublisher,
    PublicationState,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope, PublicationLedger
from agent_content_pipeline.workspace import (
    ArtifactIntegrityError,
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


def create_verified_product(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="文章工作流测试",
            slug="test-article",
            created_on=date(2026, 8, 10),
        )
    )
    style = "font-size: 16px; color: #222; line-height: 1.8; text-align: left"
    body = (
        f'<p style="{style}">斩我斋：测试文章</p>'
        f'<p style="{style}">2026.08.10</p>'
        f'<p style="{style}">正文</p>'
    )
    article = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={
                "article.mdx": (
                    '---\ntitle: "斩我斋：测试文章"\ndate: 2026-08-10\n'
                    'category: essay\ntags: ["AI"]\nsummary: "摘要"\n---\n\n正文\n'
                ).encode(),
                "body.html": body.encode(),
                "index.html": f"<html><body>{body}</body></html>".encode(),
            },
        ),
    )
    cover = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.COVER,
            files={"cover.png": png_fixture()},
        ),
    )
    return workspace, product, article, cover


def record_exact_approvals(ledger, article, cover, target_slug="test-article"):
    ledger.record(ApprovalScope.ARTICLE, article.revision, article.digest)
    ledger.record(ApprovalScope.COVER, cover.revision, cover.digest)
    key = article_publication_approval_key(
        article.revision,
        cover.revision,
        target_slug,
        True,
    )
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        key,
        article_publication_content_digest(
            article.digest,
            cover.digest,
            target_slug,
            True,
        ),
    )


def workflow_for(workspace, product, publisher):
    return ArticlePublicationWorkflow(
        ApprovalLedger(product.root),
        publisher,
        workspace,
        product,
    )


def publish_v001(workflow):
    return workflow.publish(
        article_revision="v001",
        cover_revision="v001",
    )


def test_pipeline_never_calls_the_vps_without_all_exact_approvals(tmp_path):
    workspace, product, _, _ = create_verified_product(tmp_path)
    calls = 0

    def must_not_run(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": True}, request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(must_not_run)),
    )

    with pytest.raises(ApprovalRequired) as error:
        publish_v001(workflow_for(workspace, product, publisher))

    assert error.value.missing == (
        "article:v001",
        "cover:v001",
        "article-publication:v001+v001+test-article+wechat",
    )
    assert calls == 0


def test_pipeline_calls_the_vps_once_after_all_exact_approvals(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    calls = 0

    def succeed(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": True, "wechatPushed": True},
            request=request,
        )

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(succeed)),
    )
    record_exact_approvals(ApprovalLedger(product.root), article, cover)
    workflow = workflow_for(workspace, product, publisher)

    assert publish_v001(workflow).state == "succeeded"
    with pytest.raises(AlreadyPublished):
        publish_v001(workflow)
    assert calls == 1


def test_pipeline_never_blindly_retries_partial_article_publication(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    calls = 0

    def partial(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": True, "wechatPushed": False, "wechatError": "failed"},
            request=request,
        )

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(partial)),
    )
    record_exact_approvals(ApprovalLedger(product.root), article, cover)
    workflow = workflow_for(workspace, product, publisher)

    assert publish_v001(workflow).state == "partial"
    with pytest.raises(UnsafeToRetry) as error:
        publish_v001(workflow)
    assert error.value.prior_state == "partial"
    assert calls == 1


def test_concurrent_agents_cannot_claim_the_same_article_publication(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    entered_remote = Event()
    release_remote = Event()
    call_lock = Lock()
    calls = 0

    def succeed(request):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            entered_remote.set()
            assert release_remote.wait(timeout=5)
        return httpx.Response(
            200,
            json={"success": True, "wechatPushed": True},
            request=request,
        )

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(succeed)),
    )
    record_exact_approvals(ApprovalLedger(product.root), article, cover)
    first = workflow_for(workspace, product, publisher)
    second = workflow_for(workspace, product, publisher)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_result = executor.submit(publish_v001, first)
        assert entered_remote.wait(timeout=5)
        try:
            with pytest.raises(UnsafeToRetry) as error:
                publish_v001(second)
            assert error.value.prior_state == "running"
        finally:
            release_remote.set()
        assert first_result.result(timeout=5).state == "succeeded"
    assert calls == 1


def test_pipeline_rejects_revision_only_approval_without_content_digest(tmp_path):
    workspace, product, _, _ = create_verified_product(tmp_path)
    ledger = ApprovalLedger(product.root)
    ledger.record(ApprovalScope.ARTICLE, "v001")
    ledger.record(ApprovalScope.COVER, "v001")
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "test-article", True),
    )
    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
    )

    with pytest.raises(ApprovalRequired):
        publish_v001(workflow_for(workspace, product, publisher))


def test_pipeline_detects_changed_revision_bytes_before_constructing_request(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    record_exact_approvals(ApprovalLedger(product.root), article, cover)
    (article.root / "article.mdx").write_text("tampered", encoding="utf-8")

    class MustNotPublish:
        def preview(self, spec):
            raise AssertionError("tampered bytes must not reach publisher")

        def publish(self, preview):
            raise AssertionError("tampered bytes must not reach publisher")

    with pytest.raises(ArtifactIntegrityError):
        publish_v001(workflow_for(workspace, product, MustNotPublish()))


def test_pipeline_article_bundle_keeps_the_verified_bytes_for_preview_and_approval(tmp_path):
    workspace, product, article, _cover = create_verified_product(tmp_path)

    bundle = load_article_publication_bundle(workspace, product, "v001", "v001")
    (article.root / "article.mdx").write_text("tampered-after-load", encoding="utf-8")

    assert "斩我斋：测试文章" in bundle.markdown
    assert "tampered-after-load" not in bundle.markdown
    assert bundle.article_digest == article.digest


def test_pipeline_publishes_the_verified_snapshot_if_files_change_after_read(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    record_exact_approvals(ApprovalLedger(product.root), article, cover)
    seen_markdown = []

    class MutatingPublisher:
        def preview(self, spec):
            seen_markdown.append(spec.markdown)
            (article.root / "article.mdx").write_text("tampered-after-snapshot", encoding="utf-8")
            return ArticlePublishPreview(
                endpoint="https://example.com/api/articles",
                source_slug=spec.source_slug,
                target_slug=spec.target_slug,
                duplicate_site=False,
                request_body={"markdown": spec.markdown},
            )

        def publish(self, preview):
            assert "tampered-after-snapshot" not in preview.request_body["markdown"]
            return ArticlePublishResult(state=PublicationState.SUCCEEDED)

    result = publish_v001(workflow_for(workspace, product, MutatingPublisher()))

    assert result.state == PublicationState.SUCCEEDED
    assert seen_markdown and "斩我斋：测试文章" in seen_markdown[0]


def test_pipeline_resolves_claim_when_local_preview_fails_before_external_seam(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    record_exact_approvals(ApprovalLedger(product.root), article, cover)

    class BrokenPreviewPublisher:
        def preview(self, spec):
            raise ValueError("local preview failed")

        def publish(self, preview):
            raise AssertionError("external seam must not be crossed")

    with pytest.raises(ValueError, match="local preview failed"):
        publish_v001(workflow_for(workspace, product, BrokenPreviewPublisher()))

    key = article_publication_approval_key("v001", "v001", "test-article", True)
    assert PublicationLedger(product.root).get_state("website-wechat", key) == "failed"


def test_pipeline_resolves_claim_as_unknown_when_external_adapter_raises(tmp_path):
    workspace, product, article, cover = create_verified_product(tmp_path)
    record_exact_approvals(ApprovalLedger(product.root), article, cover)

    class BrokenExternalPublisher:
        def preview(self, spec):
            return object()

        def publish(self, preview):
            raise RuntimeError("connection disappeared")

    with pytest.raises(RuntimeError, match="connection disappeared"):
        publish_v001(workflow_for(workspace, product, BrokenExternalPublisher()))

    key = article_publication_approval_key("v001", "v001", "test-article", True)
    assert PublicationLedger(product.root).get_state("website-wechat", key) == "unknown"


def test_publication_ledger_rejects_unknown_state_values(tmp_path):
    with pytest.raises(ValueError):
        PublicationLedger(tmp_path).record_state("website-wechat", "key", "typo")
