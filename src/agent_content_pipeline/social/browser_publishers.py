from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .models import (
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialPublishResult,
)


class BrowserPage(Protocol):
    def navigate(self, url: str) -> None: ...
    def exists(self, selector: str) -> bool: ...
    def fill(self, selector: str, value: str) -> None: ...
    def set_files(self, selector: str, paths: Sequence[Path]) -> None: ...
    def evaluate(self, expression: str) -> Any: ...


@dataclass(frozen=True)
class PlatformUiContract:
    platform: SocialPlatform
    upload_url: str
    file_inputs: tuple[str, ...]
    title_inputs: tuple[str, ...]
    body_inputs: tuple[str, ...]
    login_markers: tuple[str, ...]
    submit_texts: tuple[str, ...]
    success_texts: tuple[str, ...]


CONTRACTS = {
    SocialPlatform.XIAOHONGSHU: PlatformUiContract(
        platform=SocialPlatform.XIAOHONGSHU,
        upload_url="https://creator.xiaohongshu.com/publish/publish?source=official",
        file_inputs=("input[type=file][accept*='video']", "input[type=file]"),
        title_inputs=("input[placeholder*='标题']", "input.d-text"),
        body_inputs=("div[contenteditable=true]", "textarea[placeholder*='正文']"),
        login_markers=(".login-container", "[class*='login'] canvas"),
        submit_texts=("发布",),
        success_texts=("发布成功", "提交成功"),
    ),
    SocialPlatform.DOUYIN: PlatformUiContract(
        platform=SocialPlatform.DOUYIN,
        upload_url="https://creator.douyin.com/creator-micro/content/upload",
        file_inputs=("input[type=file][accept*='video']", "input[type=file]"),
        title_inputs=("input[placeholder*='标题']", "input[placeholder*='作品标题']"),
        body_inputs=("div[contenteditable=true]", "textarea[placeholder*='描述']"),
        login_markers=("[class*='login']", "[class*='qrcode']"),
        submit_texts=("发布", "立即发布"),
        success_texts=("发布成功", "投稿成功"),
    ),
    SocialPlatform.BILIBILI: PlatformUiContract(
        platform=SocialPlatform.BILIBILI,
        upload_url="https://member.bilibili.com/platform/upload/video/frame",
        file_inputs=("input[type=file][accept*='video']", "input[type=file]"),
        title_inputs=("input[placeholder*='标题']", ".video-title input"),
        body_inputs=("textarea[placeholder*='简介']", "div[contenteditable=true]"),
        login_markers=(".login-tip", "[class*='login']"),
        submit_texts=("立即投稿", "投稿"),
        success_texts=("投稿成功", "稿件投递成功"),
    ),
}


class VisibleChromePlatformPublisher:
    """Shared synchronous submit flow; selectors stay in one replaceable contract."""

    def __init__(self, platform: SocialPlatform, confirmation_timeout_seconds: float = 60) -> None:
        self.contract = CONTRACTS[platform]
        self._confirmation_timeout_seconds = confirmation_timeout_seconds

    def publish(self, page: BrowserPage, spec: SocialPostSpec) -> SocialPublishResult:
        if spec.platform != self.contract.platform:
            raise ValueError("post platform does not match publisher")
        page.navigate(self.contract.upload_url)
        deadline = time.monotonic() + 20
        file_input = None
        login_visible = False
        while time.monotonic() < deadline:
            file_input = self._first_existing(page, self.contract.file_inputs)
            if file_input:
                break
            login_visible = bool(
                self._first_existing(page, self.contract.login_markers)
                or page.evaluate(
                    "(() => { const text = document.body?.innerText || ''; "
                    "return text.includes('登录') || text.includes('扫码'); })()"
                )
            )
            if login_visible:
                break
            time.sleep(0.5)
        if file_input is None:
            if login_visible:
                return SocialPublishResult(
                    platform=spec.platform,
                    state=SocialPublicationState.WAITING_FOR_USER,
                    message=(
                        f"请在已打开的 {spec.platform.value} 专用 Chrome 窗口完成登录；"
                        "登录态只保存在该平台的本地浏览器 Profile。"
                    ),
                )
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.UNKNOWN,
                message="upload control was not found; the platform page may have changed",
            )

        page.set_files(file_input, [spec.video_path])
        title_input = self._wait_for_any(page, self.contract.title_inputs, 120)
        body_input = self._first_existing(page, self.contract.body_inputs)
        if title_input is None:
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.UNKNOWN,
                message="video upload started but the title editor did not become available",
            )
        page.fill(title_input, spec.title)
        if body_input:
            topics = " ".join(f"#{tag.lstrip('#')}" for tag in spec.tags)
            body = " ".join(part for part in (spec.body.strip(), topics) if part)
            page.fill(body_input, body)
        if spec.platform == SocialPlatform.BILIBILI and spec.category:
            self._select_text(page, spec.category)
        if not self._click_text(page, self.contract.submit_texts):
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.WAITING_FOR_USER,
                message="metadata is filled; review required options and click publish in the visible window",
            )
        if self._wait_for_text(page, self.contract.success_texts):
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.SUBMITTED,
                message="platform explicitly confirmed that the submission was accepted",
            )
        return SocialPublishResult(
            platform=spec.platform,
            state=SocialPublicationState.UNKNOWN,
            message="publish was clicked but no explicit confirmation appeared; reconcile before retrying",
        )

    @staticmethod
    def _first_existing(page: BrowserPage, selectors: Sequence[str]) -> str | None:
        return next((selector for selector in selectors if page.exists(selector)), None)

    def _wait_for_any(
        self,
        page: BrowserPage,
        selectors: Sequence[str],
        timeout_seconds: float,
    ) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            found = self._first_existing(page, selectors)
            if found:
                return found
            time.sleep(0.5)
        return None

    @staticmethod
    def _click_text(page: BrowserPage, texts: Sequence[str]) -> bool:
        return bool(
            page.evaluate(
                """
(() => {
  const texts = %s;
  const candidates = [...document.querySelectorAll('button,[role=button]')];
  const button = candidates.find(node => texts.some(text => node.textContent.trim() === text));
  if (!button) return false;
  button.scrollIntoView({block: 'center'});
  button.click();
  return true;
})()
""".strip()
                % json.dumps(list(texts), ensure_ascii=False)
            )
        )

    @staticmethod
    def _select_text(page: BrowserPage, text: str) -> bool:
        return bool(
            page.evaluate(
                """
(() => {
  const wanted = %s;
  const node = [...document.querySelectorAll('[role=option],li,span')]
    .find(item => item.textContent.trim() === wanted);
  if (!node) return false;
  node.click();
  return true;
})()
""".strip()
                % json.dumps(text, ensure_ascii=False)
            )
        )

    def _wait_for_text(self, page: BrowserPage, texts: Sequence[str]) -> bool:
        deadline = time.monotonic() + self._confirmation_timeout_seconds
        expression = "texts => texts.some(text => document.body.innerText.includes(text))"
        while time.monotonic() < deadline:
            found = page.evaluate(
                f"({expression})({json.dumps(list(texts), ensure_ascii=False)})"
            )
            if found:
                return True
            time.sleep(0.5)
        return False
