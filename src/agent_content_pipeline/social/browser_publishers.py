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
    def read_value(self, selector: str) -> str | None: ...
    def fill_tags(
        self,
        selector: str,
        tags: Sequence[str],
        chip_selectors: Sequence[str],
    ) -> bool: ...
    def set_files(self, selector: str, paths: Sequence[Path]) -> None: ...
    def evaluate(self, expression: str) -> Any: ...


@dataclass(frozen=True)
class PlatformUiContract:
    platform: SocialPlatform
    upload_url: str
    file_inputs: tuple[str, ...]
    title_inputs: tuple[str, ...]
    body_inputs: tuple[str, ...]
    tag_inputs: tuple[str, ...]
    tag_chips: tuple[str, ...]
    category_controls: tuple[str, ...]
    category_options: tuple[str, ...]
    category_selected_values: tuple[str, ...]
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
        tag_inputs=(),
        tag_chips=(),
        category_controls=(),
        category_options=(),
        category_selected_values=(),
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
        tag_inputs=(),
        tag_chips=(),
        category_controls=(),
        category_options=(),
        category_selected_values=(),
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
        tag_inputs=(
            "input[placeholder*='标签']",
            ".tag-input input",
            "input[placeholder*='Enter']",
        ),
        tag_chips=(
            "[data-tag]",
            "[class*='tag-item']:has([class*='close'])",
            "[class*='tag-item']:has([class*='delete'])",
            "[class*='tag']:has(button[aria-label*='删除'])",
        ),
        category_controls=(
            "[class*='partition'] [role=combobox]",
            "[class*='partition'] [class*='select']",
            "[class*='type'] [role=combobox]",
            "[class*='type'] [class*='select']",
        ),
        category_options=(
            "[role=listbox] [role=option]",
            "[class*='dropdown'] [class*='option']",
            ".bcc-select-dropdown li",
        ),
        category_selected_values=(
            "input",
            "[aria-selected='true']",
            "[class*='selected']",
            "[class*='selection']",
            "[class*='value']",
        ),
        login_markers=(".login-tip", "[class*='login']"),
        submit_texts=("立即投稿", "投稿"),
        success_texts=("投稿成功", "稿件投递成功"),
    ),
}


class VisibleChromePlatformPublisher:
    """Shared synchronous submit flow; selectors stay in one replaceable contract."""

    def __init__(
        self,
        platform: SocialPlatform,
        confirmation_timeout_seconds: float = 60,
        editor_timeout_seconds: float = 120,
    ) -> None:
        self.contract = CONTRACTS[platform]
        self._confirmation_timeout_seconds = confirmation_timeout_seconds
        self._editor_timeout_seconds = editor_timeout_seconds

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
        title_input = self._wait_for_any(
            page,
            self.contract.title_inputs,
            self._editor_timeout_seconds,
        )
        if title_input is None:
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.UNKNOWN,
                message="video upload started but the title editor did not become available",
            )
        body_input = self._wait_for_any(
            page,
            self.contract.body_inputs,
            self._editor_timeout_seconds,
        )
        if body_input is None:
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.FAILED,
                message="approved body and tags were not submitted because the body control is missing",
            )
        if not self._fill_and_verify(page, title_input, spec.title):
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.FAILED,
                message="approved title could not be read back exactly; publication was not submitted",
            )
        if spec.platform == SocialPlatform.BILIBILI:
            if not self._fill_and_verify(page, body_input, spec.body.strip()):
                return SocialPublishResult(
                    platform=spec.platform,
                    state=SocialPublicationState.FAILED,
                    message="approved body could not be read back exactly; publication was not submitted",
                )
            tag_input = self._wait_for_any(
                page,
                self.contract.tag_inputs,
                self._editor_timeout_seconds,
            )
            if tag_input is None:
                return SocialPublishResult(
                    platform=spec.platform,
                    state=SocialPublicationState.FAILED,
                    message="approved bilibili tags were not submitted because the tag control is missing",
                )
            if not page.fill_tags(tag_input, spec.tags, self.contract.tag_chips):
                return SocialPublishResult(
                    platform=spec.platform,
                    state=SocialPublicationState.FAILED,
                    message=(
                        "approved bilibili tags could not be verified in the tag control; "
                        "publication was not submitted"
                    ),
                )
        else:
            topics = " ".join(f"#{tag.lstrip('#')}" for tag in spec.tags)
            body = " ".join(part for part in (spec.body.strip(), topics) if part)
            if not self._fill_and_verify(page, body_input, body):
                return SocialPublishResult(
                    platform=spec.platform,
                    state=SocialPublicationState.FAILED,
                    message=(
                        "approved body and tags could not be read back exactly; "
                        "publication was not submitted"
                    ),
                )
        if (
            spec.platform == SocialPlatform.BILIBILI
            and spec.category
            and not self._select_category(page, spec.category)
        ):
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.FAILED,
                message="approved bilibili category could not be selected; publication was not submitted",
            )
        if not self._click_text(page, self.contract.submit_texts):
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.WAITING_FOR_USER,
                message=(
                    "metadata is filled but required options still need attention; "
                    "do not click publish, complete the options in the visible window, "
                    "then retry only this platform stage"
                ),
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

    def _fill_and_verify(
        self,
        page: BrowserPage,
        selector: str,
        approved: str,
    ) -> bool:
        page.fill(selector, approved)
        deadline = time.monotonic() + min(self._editor_timeout_seconds, 5)
        normalized_approved = self._normalize_metadata(approved)
        while time.monotonic() < deadline:
            actual = page.read_value(selector)
            if actual is not None and self._normalize_metadata(actual) == normalized_approved:
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _normalize_metadata(value: str) -> str:
        return " ".join(value.split())

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

    def _select_category(self, page: BrowserPage, text: str) -> bool:
        control_selector = self._wait_for_any(
            page,
            self.contract.category_controls,
            self._editor_timeout_seconds,
        )
        if control_selector is None:
            return False
        clicked = bool(
            page.evaluate(
                """
(() => {
  const control = document.querySelector(%s);
  if (!control) return false;
  const rect = control.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  control.click();
  return true;
})()
""".strip()
                % json.dumps(control_selector)
            )
        )
        if not clicked:
            return False
        choose_option = """
(() => {
  const wanted = %s;
  const normalize = value => value.replace(/\\s+/g, ' ').trim();
  const optionSelectors = %s;
  const options = optionSelectors.flatMap(selector => [...document.querySelectorAll(selector)]);
  const option = options.find(node => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0
      && normalize(node.textContent || '') === normalize(wanted);
  });
  if (!option) return false;
  option.click();
  return true;
})()
""".strip() % (
            json.dumps(text, ensure_ascii=False),
            json.dumps(list(self.contract.category_options), ensure_ascii=False),
        )
        deadline = time.monotonic() + min(self._editor_timeout_seconds, 5)
        while time.monotonic() < deadline:
            if bool(page.evaluate(choose_option)):
                break
            time.sleep(0.2)
        else:
            return False
        verification = """
(() => {
  const control = document.querySelector(%s);
  if (!control) return false;
  const wanted = %s;
  const normalize = value => value.replace(/\\s+/g, ' ').trim();
  const selectedSelectors = %s;
  const selectedValues = [control, ...selectedSelectors.flatMap(
    selector => [...control.querySelectorAll(selector)]
  )];
  return selectedValues.some(node => {
    const value = node instanceof HTMLInputElement ? node.value : node.textContent || '';
    return normalize(value) === normalize(wanted);
  });
})()
""".strip() % (
            json.dumps(control_selector),
            json.dumps(text, ensure_ascii=False),
            json.dumps(list(self.contract.category_selected_values), ensure_ascii=False),
        )
        deadline = time.monotonic() + min(self._editor_timeout_seconds, 5)
        while time.monotonic() < deadline:
            if bool(page.evaluate(verification)):
                return True
            time.sleep(0.2)
        return False

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
