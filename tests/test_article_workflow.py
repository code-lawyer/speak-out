import httpx
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from agent_content_pipeline.pipeline import (
    AlreadyPublished,
    ApprovalRequired,
    ArticlePublicationEvidence,
    ArticlePublicationWorkflow,
    UnsafeToRetry,
    article_publication_approval_key,
    article_publication_content_digest,
)
from agent_content_pipeline.publishing.article import (
    ArticlePublicationSpec,
    FixedIpVpsPublisher,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope, PublicationLedger


EVIDENCE = ArticlePublicationEvidence(
    article_revision="v001",
    article_digest="a" * 64,
    cover_revision="v001",
    cover_digest="b" * 64,
)


def record_exact_approvals(ledger: ApprovalLedger, target_slug: str = "test-article") -> None:
    ledger.record(ApprovalScope.ARTICLE, EVIDENCE.article_revision, EVIDENCE.article_digest)
    ledger.record(ApprovalScope.COVER, EVIDENCE.cover_revision, EVIDENCE.cover_digest)
    key = article_publication_approval_key(
        EVIDENCE.article_revision,
        EVIDENCE.cover_revision,
        target_slug,
        True,
    )
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        key,
        article_publication_content_digest(
            EVIDENCE.article_digest,
            EVIDENCE.cover_digest,
            target_slug,
            True,
        ),
    )


def test_pipeline_never_calls_the_vps_without_all_exact_approvals(tmp_path):
    calls = 0

    def must_not_run(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": True}, request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(must_not_run)),
    )
    workflow = ArticlePublicationWorkflow(ApprovalLedger(tmp_path), publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    with pytest.raises(ApprovalRequired) as error:
        workflow.publish(spec, evidence=EVIDENCE)

    assert error.value.missing == (
        "article:v001",
        "cover:v001",
        "article-publication:v001+v001+test-article+wechat",
    )
    assert calls == 0


def test_pipeline_calls_the_vps_once_after_all_exact_approvals(tmp_path):
    calls = 0

    def succeed(request: httpx.Request) -> httpx.Response:
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
    ledger = ApprovalLedger(tmp_path)
    record_exact_approvals(ledger)
    workflow = ArticlePublicationWorkflow(ledger, publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    result = workflow.publish(spec, evidence=EVIDENCE)

    assert result.state == "succeeded"
    assert calls == 1

    with pytest.raises(AlreadyPublished):
        workflow.publish(spec, evidence=EVIDENCE)

    assert calls == 1


def test_pipeline_never_blindly_retries_partial_article_publication(tmp_path):
    calls = 0

    def partial(request: httpx.Request) -> httpx.Response:
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
    ledger = ApprovalLedger(tmp_path)
    record_exact_approvals(ledger)
    workflow = ArticlePublicationWorkflow(ledger, publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    assert workflow.publish(spec, evidence=EVIDENCE).state == "partial"
    with pytest.raises(UnsafeToRetry) as error:
        workflow.publish(spec, evidence=EVIDENCE)

    assert error.value.prior_state == "partial"
    assert calls == 1


def test_concurrent_agents_cannot_claim_the_same_article_publication(tmp_path):
    entered_remote = Event()
    release_remote = Event()
    call_lock = Lock()
    calls = 0

    def succeed(request: httpx.Request) -> httpx.Response:
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
    approvals = ApprovalLedger(tmp_path)
    record_exact_approvals(approvals)
    first = ArticlePublicationWorkflow(approvals, publisher)
    second = ArticlePublicationWorkflow(approvals, publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_result = executor.submit(
            first.publish,
            spec,
            evidence=EVIDENCE,
        )
        assert entered_remote.wait(timeout=5)
        try:
            with pytest.raises(UnsafeToRetry) as error:
                second.publish(spec, evidence=EVIDENCE)
            assert error.value.prior_state == "running"
        finally:
            release_remote.set()
        assert first_result.result(timeout=5).state == "succeeded"

    assert calls == 1


def test_pipeline_rejects_revision_only_approval_without_exact_content_digest(tmp_path):
    ledger = ApprovalLedger(tmp_path)
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
    spec = ArticlePublicationSpec(
        markdown="article",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>article</p>",
        cover_png=b"png",
    )

    with pytest.raises(ApprovalRequired):
        ArticlePublicationWorkflow(ledger, publisher).publish(spec, evidence=EVIDENCE)


def test_pipeline_resolves_claim_when_local_preview_fails_before_external_seam(tmp_path):
    class BrokenPreviewPublisher:
        def preview(self, spec):
            raise ValueError("local preview failed")

        def publish(self, preview):
            raise AssertionError("external seam must not be crossed")

    ledger = ApprovalLedger(tmp_path)
    record_exact_approvals(ledger)
    spec = ArticlePublicationSpec(
        markdown="article",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>article</p>",
        cover_png=b"png",
    )

    with pytest.raises(ValueError, match="local preview failed"):
        ArticlePublicationWorkflow(ledger, BrokenPreviewPublisher()).publish(
            spec,
            evidence=EVIDENCE,
        )

    key = article_publication_approval_key("v001", "v001", "test-article", True)
    assert PublicationLedger(tmp_path).get_state("website-wechat", key) == "failed"


def test_pipeline_resolves_claim_as_unknown_when_external_adapter_raises(tmp_path):
    class BrokenExternalPublisher:
        def preview(self, spec):
            return object()

        def publish(self, preview):
            raise RuntimeError("connection disappeared")

    ledger = ApprovalLedger(tmp_path)
    record_exact_approvals(ledger)
    spec = ArticlePublicationSpec(
        markdown="article",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>article</p>",
        cover_png=b"png",
    )

    with pytest.raises(RuntimeError, match="connection disappeared"):
        ArticlePublicationWorkflow(ledger, BrokenExternalPublisher()).publish(
            spec,
            evidence=EVIDENCE,
        )

    key = article_publication_approval_key("v001", "v001", "test-article", True)
    assert PublicationLedger(tmp_path).get_state("website-wechat", key) == "unknown"
