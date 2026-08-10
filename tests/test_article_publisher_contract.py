import base64

import pytest
import httpx

from agent_content_pipeline.publishing.article import (
    ArticlePublicationSpec,
    ArticleValidationError,
    FixedIpVpsPublisher,
)


def test_article_preview_preserves_the_proven_vps_request_contract():
    cover = b"\x89PNG\r\n\x1a\ncontract-fixture"
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html='<p style="font-size:16px">测试</p>',
        cover_png=cover,
        push_to_wechat=True,
    )

    preview = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="must-not-appear-in-preview",
    ).preview(spec)

    assert preview.endpoint == "https://hillward.top/api/articles"
    assert preview.request_body == {
        "markdown": spec.markdown,
        "slug": "test-article",
        "wechatHTML": spec.wechat_html,
        "pushToWechat": True,
        "coverImageBase64": (
            "data:image/png;base64," + base64.b64encode(cover).decode("ascii")
        ),
    }
    assert "must-not-appear-in-preview" not in preview.model_dump_json()


def test_wechat_preview_refuses_to_continue_without_a_cover():
    spec = ArticlePublicationSpec(
        markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
        source_slug="test-article",
        target_slug="test-article",
        wechat_html="<p>测试</p>",
        cover_png=None,
        push_to_wechat=True,
    )

    with pytest.raises(ArticleValidationError) as error:
        FixedIpVpsPublisher(
            endpoint="https://hillward.top/api/articles",
            bearer_token="secret",
        ).preview(spec)

    assert error.value.issues == ("WeChat cover is required",)


def test_article_publish_posts_the_contract_without_exposing_the_secret():
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"success": True, "wechatPushed": True},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="local-test-secret",
        http_client=client,
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>测试</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert captured["authorization"] == "Bearer local-test-secret"
    assert result.state == "succeeded"
    assert result.http_status == 200
    assert result.response == {"success": True, "wechatPushed": True}
    assert "local-test-secret" not in result.model_dump_json()


def test_article_publish_timeout_is_unknown_instead_of_safe_to_retry():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response was lost", request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="local-test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: 斩我斋：测试文章\n---\n正文\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>测试</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.state == "unknown"
    assert result.http_status is None
    assert result.error == "request timed out; remote publish state is unknown"


def test_article_publish_response_disconnect_is_unknown_instead_of_safe_to_retry():
    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("response connection was lost", request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="local-test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(disconnect)),
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: test\n---\nbody\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>test</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.state == "unknown"
    assert result.error == "request connection was lost; remote publish state is unknown"


def test_http_200_without_explicit_business_success_is_unknown():
    def ambiguous(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "accepted"}, request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(ambiguous)),
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: test\n---\nbody\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>test</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.state == "unknown"


def test_server_error_without_explicit_business_result_is_unknown():
    def ambiguous_gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway", request=request)

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(ambiguous_gateway)),
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: test\n---\nbody\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>test</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.state == "unknown"
    assert result.http_status == 502


def test_non_json_response_body_is_not_exposed_in_result_or_logs():
    def private_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            text="Set-Cookie: session=private-cookie-value",
            request=request,
        )

    publisher = FixedIpVpsPublisher(
        endpoint="https://hillward.top/api/articles",
        bearer_token="secret",
        http_client=httpx.Client(transport=httpx.MockTransport(private_error)),
    )
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: test\n---\nbody\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>test</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.response == {"raw": "[NON-JSON RESPONSE OMITTED]"}
    assert "private-cookie-value" not in result.model_dump_json()


def test_site_success_with_explicit_wechat_failure_is_partial():
    def partial(request: httpx.Request) -> httpx.Response:
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
    preview = publisher.preview(
        ArticlePublicationSpec(
            markdown="---\ntitle: test\n---\nbody\n",
            source_slug="test-article",
            target_slug="test-article",
            wechat_html="<p>test</p>",
            cover_png=b"png",
            push_to_wechat=True,
        )
    )

    result = publisher.publish(preview)

    assert result.state == "partial"
    assert "local-test-secret" not in result.model_dump_json()
