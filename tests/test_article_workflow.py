import httpx
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from agent_content_pipeline.pipeline import (
    AlreadyPublished,
    ApprovalRequired,
    ArticlePublicationWorkflow,
    UnsafeToRetry,
    article_publication_approval_key,
)
from agent_content_pipeline.publishing.article import (
    ArticlePublicationSpec,
    FixedIpVpsPublisher,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope


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
        workflow.publish(spec, article_revision="v001", cover_revision="v001")

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
    ledger.record(ApprovalScope.ARTICLE, "v001")
    ledger.record(ApprovalScope.COVER, "v001")
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "test-article", True),
    )
    workflow = ArticlePublicationWorkflow(ledger, publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    result = workflow.publish(spec, article_revision="v001", cover_revision="v001")

    assert result.state == "succeeded"
    assert calls == 1

    with pytest.raises(AlreadyPublished):
        workflow.publish(spec, article_revision="v001", cover_revision="v001")

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
    ledger.record(ApprovalScope.ARTICLE, "v001")
    ledger.record(ApprovalScope.COVER, "v001")
    ledger.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "test-article", True),
    )
    workflow = ArticlePublicationWorkflow(ledger, publisher)
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=b"png",
        push_to_wechat=True,
    )

    assert workflow.publish(spec, article_revision="v001", cover_revision="v001").state == "partial"
    with pytest.raises(UnsafeToRetry) as error:
        workflow.publish(spec, article_revision="v001", cover_revision="v001")

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
    approvals.record(ApprovalScope.ARTICLE, "v001")
    approvals.record(ApprovalScope.COVER, "v001")
    approvals.record(
        ApprovalScope.ARTICLE_PUBLICATION,
        article_publication_approval_key("v001", "v001", "test-article", True),
    )
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
            article_revision="v001",
            cover_revision="v001",
        )
        assert entered_remote.wait(timeout=5)
        try:
            with pytest.raises(UnsafeToRetry) as error:
                second.publish(spec, article_revision="v001", cover_revision="v001")
            assert error.value.prior_state == "running"
        finally:
            release_remote.set()
        assert first_result.result(timeout=5).state == "succeeded"

    assert calls == 1
