from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ArticlePublicationSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    markdown: str = Field(min_length=1)
    source_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    target_slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    wechat_html: str | None = None
    cover_png: bytes | None = None
    push_to_wechat: bool = True


class ArticlePublishPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    source_slug: str
    target_slug: str
    duplicate_site: bool
    request_body: dict[str, Any]


class PublicationState(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ArticlePublishResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: PublicationState
    http_status: int | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ArticleChannelReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    site: str
    cover: str
    wechat: str


def interpret_article_result(result: ArticlePublishResult) -> ArticleChannelReport:
    """Only claim channel success when the VPS response explicitly proves it."""

    if result.state == PublicationState.UNKNOWN:
        return ArticleChannelReport(site="unknown", cover="unknown", wechat="unknown")

    success = result.response.get("success")
    site = "published" if success is True else "failed" if success is False else "unknown"
    wechat_pushed = result.response.get("wechatPushed")
    if wechat_pushed is True:
        return ArticleChannelReport(site=site, cover="uploaded", wechat="drafted")
    if wechat_pushed is False and result.response.get("wechatError"):
        return ArticleChannelReport(site=site, cover="unknown", wechat="failed")
    return ArticleChannelReport(site=site, cover="unknown", wechat="unknown")


def redact_publication_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(r"(?:secret|token|password|authorization|base64)", key, re.IGNORECASE)
                else redact_publication_data(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_publication_data(item) for item in value]
    return value


def write_article_publish_log(
    product_root: Path,
    preview: ArticlePublishPreview,
    result: ArticlePublishResult,
    channels: ArticleChannelReport,
) -> Path:
    log_root = product_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC)
    filename = timestamp.isoformat().replace(":", "-").replace(".", "-")
    path = log_root / f"{filename}-{preview.target_slug}.json"
    payload = {
        "timestamp": timestamp.isoformat(),
        "destination": "website-wechat",
        "endpoint": preview.endpoint,
        "sourceSlug": preview.source_slug,
        "slug": preview.target_slug,
        "duplicateSite": preview.duplicate_site,
        "state": result.state.value,
        "httpStatus": result.http_status,
        "site": channels.site,
        "cover": channels.cover,
        "wechat": channels.wechat,
        "response": redact_publication_data(result.response),
        "error": result.error,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class ArticleValidationError(ValueError):
    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


class FixedIpVpsPublisher:
    """Preserve the proven site/WeChat VPS request contract."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.endpoint = endpoint
        self._bearer_token = bearer_token
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    def preview(self, spec: ArticlePublicationSpec) -> ArticlePublishPreview:
        if spec.push_to_wechat and spec.cover_png is None:
            raise ArticleValidationError(("WeChat cover is required",))

        request_body: dict[str, Any] = {
            "markdown": spec.markdown,
            "slug": spec.target_slug,
            "pushToWechat": spec.push_to_wechat,
        }
        if spec.wechat_html is not None:
            request_body["wechatHTML"] = spec.wechat_html
        if spec.cover_png is not None:
            encoded = base64.b64encode(spec.cover_png).decode("ascii")
            request_body["coverImageBase64"] = f"data:image/png;base64,{encoded}"

        return ArticlePublishPreview(
            endpoint=self.endpoint,
            source_slug=spec.source_slug,
            target_slug=spec.target_slug,
            duplicate_site=spec.source_slug != spec.target_slug,
            request_body=request_body,
        )

    def publish(self, preview: ArticlePublishPreview) -> ArticlePublishResult:
        http_client = self._http_client or httpx.Client()
        try:
            response = http_client.post(
                preview.endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bearer_token}",
                },
                json=preview.request_body,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException:
            return ArticlePublishResult(
                state=PublicationState.UNKNOWN,
                error="request timed out; remote publish state is unknown",
            )
        try:
            response_body = response.json()
        except ValueError:
            response_body = {"raw": response.text}
        if not response.is_success or response_body.get("success") is False:
            state = PublicationState.FAILED
        elif response_body.get("success") is not True:
            state = PublicationState.UNKNOWN
        elif preview.request_body.get("pushToWechat") is not True:
            state = PublicationState.SUCCEEDED
        elif response_body.get("wechatPushed") is True:
            state = PublicationState.SUCCEEDED
        elif response_body.get("wechatPushed") is False:
            state = PublicationState.PARTIAL
        else:
            state = PublicationState.UNKNOWN
        return ArticlePublishResult(
            state=state,
            http_status=response.status_code,
            response=response_body,
        )
