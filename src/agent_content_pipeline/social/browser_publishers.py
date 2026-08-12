from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlparse

from .models import (
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialPublishResult,
    SocialUploadState,
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


@dataclass
class BrowserPublishLifecycle:
    """Tracks browser upload separately from the irreversible submit seam."""

    upload_state: SocialUploadState = SocialUploadState.NOT_STARTED
    submission_started: bool = False
    _on_submission_started: Callable[[], None] | None = None

    def bind_submission_start(self, callback: Callable[[], None]) -> None:
        self._on_submission_started = callback

    def mark_upload_started(self) -> None:
        self.upload_state = SocialUploadState.UPLOADING

    def mark_upload_completed(self) -> None:
        self.upload_state = SocialUploadState.COMPLETED

    def mark_upload_failed(self) -> None:
        self.upload_state = SocialUploadState.FAILED

    def mark_submission_started(self) -> None:
        if self.upload_state != SocialUploadState.COMPLETED:
            raise RuntimeError("cannot submit before the platform confirms upload completion")
        if self.submission_started:
            return
        self.submission_started = True
        if self._on_submission_started is not None:
            self._on_submission_started()


@dataclass(frozen=True)
class UploadObservation:
    state: SocialUploadState
    evidence: str
    expected_file_seen: bool = False
    remote_confirmed: bool = False


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
    upload_in_progress_texts: tuple[str, ...]
    upload_failed_texts: tuple[str, ...]
    upload_complete_texts: tuple[str, ...]
    submit_texts: tuple[str, ...]
    success_texts: tuple[str, ...]


CONTRACTS = {
    SocialPlatform.XIAOHONGSHU: PlatformUiContract(
        platform=SocialPlatform.XIAOHONGSHU,
        upload_url="https://creator.xiaohongshu.com/publish/publish?source=official",
        file_inputs=(
            "input.upload-input[type=file][accept*='.mp4']",
            "input[type=file][accept*='.mp4']",
        ),
        title_inputs=(
            "input[placeholder='填写标题会有更多赞哦']",
            "input[placeholder*='标题']",
        ),
        body_inputs=(
            "div.tiptap.ProseMirror[contenteditable=true][role=textbox]",
            "div[contenteditable=true][role=textbox]",
        ),
        tag_inputs=(),
        tag_chips=(),
        category_controls=(),
        category_options=(),
        category_selected_values=(),
        login_markers=(".login-container", "[class*='login'] canvas"),
        upload_in_progress_texts=("上传中", "视频上传中"),
        upload_failed_texts=("视频上传失败", "上传失败", "网络异常"),
        upload_complete_texts=("上传完成", "上传成功", "处理完成"),
        submit_texts=("发布",),
        success_texts=("发布成功", "提交成功"),
    ),
    SocialPlatform.DOUYIN: PlatformUiContract(
        platform=SocialPlatform.DOUYIN,
        upload_url="https://creator.douyin.com/creator-micro/content/upload",
        file_inputs=("input[type=file][accept*='video']",),
        title_inputs=(
            "input[placeholder='填写作品标题，为作品获得更多流量']",
            "input[placeholder*='作品标题']",
        ),
        body_inputs=(
            "div.editor-comp-publish[contenteditable=true]",
            "div[contenteditable=true][class*='editor']",
        ),
        tag_inputs=(),
        tag_chips=(),
        category_controls=(),
        category_options=(),
        category_selected_values=(),
        login_markers=("[class*='login']", "[class*='qrcode']"),
        upload_in_progress_texts=(
            "上传过程中请不要删除/移动文件",
            "已上传：",
            "取消上传",
        ),
        upload_failed_texts=("上传失败",),
        upload_complete_texts=("视频上传完成", "上传完成", "上传成功", "处理完成"),
        submit_texts=("发布", "立即发布"),
        success_texts=("发布成功", "投稿成功"),
    ),
    SocialPlatform.BILIBILI: PlatformUiContract(
        platform=SocialPlatform.BILIBILI,
        upload_url="https://member.bilibili.com/platform/upload/video/frame",
        file_inputs=(
            ".bcc-upload-wrapper input[type=file][accept*='.mp4']",
            "input[type=file][accept*='.mp4']",
        ),
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
        upload_in_progress_texts=("上传中", "正在上传", "暂停上传"),
        upload_failed_texts=("上传失败", "重新上传"),
        upload_complete_texts=("上传完成", "上传成功", "转码完成"),
        submit_texts=("立即投稿", "投稿"),
        success_texts=("投稿成功", "稿件投递成功"),
    ),
}


class _VisibleChromePublisher:
    """Shared implementation behind the three platform Adapter interfaces."""

    contract: PlatformUiContract

    def __init__(
        self,
        confirmation_timeout_seconds: float = 60,
        editor_timeout_seconds: float = 120,
        upload_start_timeout_seconds: float = 30,
        upload_timeout_seconds: float = 4 * 60 * 60,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self._confirmation_timeout_seconds = confirmation_timeout_seconds
        self._editor_timeout_seconds = editor_timeout_seconds
        self._upload_start_timeout_seconds = upload_start_timeout_seconds
        self._upload_timeout_seconds = upload_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    def publish(
        self,
        page: BrowserPage,
        spec: SocialPostSpec,
        *,
        lifecycle: BrowserPublishLifecycle | None = None,
    ) -> SocialPublishResult:
        if spec.platform != self.contract.platform:
            raise ValueError("post platform does not match publisher")
        lifecycle = lifecycle or BrowserPublishLifecycle()
        observation = self._observe_upload(page, spec.video_path)
        resume_current_page = self._can_resume_current_page(
            page,
            observation,
            spec.video_path,
        )
        retrying_failed_upload = (
            resume_current_page and observation.state == SocialUploadState.FAILED
        )
        if not resume_current_page:
            page.navigate(self.contract.upload_url)
            observation = UploadObservation(
                state=SocialUploadState.NOT_STARTED,
                evidence="creator upload page opened",
            )
        deadline = time.monotonic() + 20
        file_input = None
        login_visible = False
        if observation.state not in {
            SocialUploadState.UPLOADING,
            SocialUploadState.COMPLETED,
        }:
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
                time.sleep(self._poll_interval_seconds)
            if file_input is None:
                if login_visible:
                    return self._result(
                        lifecycle,
                        spec.platform,
                        SocialPublicationState.WAITING_FOR_USER,
                        (
                            f"请在已打开的 {spec.platform.value} 专用 Chrome 窗口完成登录；"
                            "登录态只保存在该平台的本地浏览器 Profile。"
                        ),
                    )
                return self._result(
                    lifecycle,
                    spec.platform,
                    SocialPublicationState.FAILED,
                    "upload control was not found; the platform page contract changed",
                )
            lifecycle.mark_upload_started()
            page.set_files(file_input, [spec.video_path])

        upload_result = self._wait_for_upload(
            page,
            spec.video_path,
            lifecycle,
            retrying_failed_upload=retrying_failed_upload,
        )
        if upload_result is not None:
            return upload_result
        title_input = self._wait_for_any(
            page,
            self.contract.title_inputs,
            self._editor_timeout_seconds,
        )
        if title_input is None:
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "platform confirmed the upload, but the title editor contract changed",
            )
        body_input = self._wait_for_any(
            page,
            self.contract.body_inputs,
            self._editor_timeout_seconds,
        )
        if body_input is None:
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "approved body and tags were not submitted because the body control is missing",
            )
        if not self._fill_and_verify(page, title_input, spec.title):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "approved title could not be read back exactly; publication was not submitted",
            )
        metadata_error = self._fill_platform_metadata(page, spec, body_input, lifecycle)
        if metadata_error is not None:
            return metadata_error
        if not self._has_click_text(page, self.contract.submit_texts):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.WAITING_FOR_USER,
                (
                    "metadata is filled but required options still need attention; "
                    "do not click publish, complete the options in the visible window, "
                    "then retry only this platform stage"
                ),
            )
        lifecycle.mark_submission_started()
        if not self._click_text(page, self.contract.submit_texts):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.UNKNOWN,
                "publish control disappeared at the submission seam; reconcile before retrying",
            )
        if self._wait_for_text(page, self.contract.success_texts):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.SUBMITTED,
                "platform explicitly confirmed that the submission was accepted",
            )
        return self._result(
            lifecycle,
            spec.platform,
            SocialPublicationState.UNKNOWN,
            "publish was clicked but no explicit confirmation appeared; reconcile before retrying",
        )

    def _fill_platform_metadata(
        self,
        page: BrowserPage,
        spec: SocialPostSpec,
        body_input: str,
        lifecycle: BrowserPublishLifecycle,
    ) -> SocialPublishResult | None:
        topics = " ".join(f"#{tag.lstrip('#')}" for tag in spec.tags)
        body = " ".join(part for part in (spec.body.strip(), topics) if part)
        if self._fill_and_verify(page, body_input, body):
            return None
        return self._result(
            lifecycle,
            spec.platform,
            SocialPublicationState.FAILED,
            (
                "approved body and tags could not be read back exactly; "
                "publication was not submitted"
            ),
        )

    def _can_resume_current_page(
        self,
        page: BrowserPage,
        observation: UploadObservation,
        video_path: Path,
    ) -> bool:
        current_url = page.evaluate("(() => location.href)()")
        if not isinstance(current_url, str):
            return False
        expected_host = urlparse(self.contract.upload_url).hostname
        current_host = urlparse(current_url).hostname
        if not expected_host or current_host != expected_host:
            return False
        if observation.state == SocialUploadState.FAILED:
            return True
        return observation.state in {
            SocialUploadState.UPLOADING,
            SocialUploadState.COMPLETED,
        } and observation.expected_file_seen

    def _wait_for_upload(
        self,
        page: BrowserPage,
        video_path: Path,
        lifecycle: BrowserPublishLifecycle,
        *,
        retrying_failed_upload: bool = False,
    ) -> SocialPublishResult | None:
        start_deadline = time.monotonic() + self._upload_start_timeout_seconds
        completion_deadline: float | None = None
        last = UploadObservation(
            state=SocialUploadState.NOT_STARTED,
            evidence="platform has not acknowledged the selected file",
        )
        while True:
            now = time.monotonic()
            if not video_path.is_file():
                lifecycle.mark_upload_failed()
                return self._result(
                    lifecycle,
                    self.contract.platform,
                    SocialPublicationState.FAILED,
                    "the private upload snapshot disappeared before upload completion",
                )
            last = self._observe_upload(page, video_path)
            if last.state == SocialUploadState.FAILED:
                if retrying_failed_upload and now < start_deadline:
                    time.sleep(self._poll_interval_seconds)
                    continue
                lifecycle.mark_upload_failed()
                return self._result(
                    lifecycle,
                    self.contract.platform,
                    SocialPublicationState.FAILED,
                    f"platform reported upload failure: {last.evidence}",
                )
            if last.state == SocialUploadState.COMPLETED:
                if not last.expected_file_seen or not last.remote_confirmed:
                    if time.monotonic() >= start_deadline:
                        lifecycle.mark_upload_failed()
                        return self._result(
                            lifecycle,
                            self.contract.platform,
                            SocialPublicationState.FAILED,
                            (
                                "platform showed upload readiness without remote completion "
                                "bound to the approved private snapshot; publication was not submitted"
                            ),
                        )
                    time.sleep(self._poll_interval_seconds)
                    continue
                lifecycle.mark_upload_completed()
                return None
            if last.state == SocialUploadState.UPLOADING:
                retrying_failed_upload = False
                lifecycle.mark_upload_started()
                if completion_deadline is None:
                    completion_deadline = now + self._upload_timeout_seconds
                if now >= completion_deadline:
                    return self._result(
                        lifecycle,
                        self.contract.platform,
                        SocialPublicationState.WAITING_FOR_USER,
                        (
                            "platform still reports the video upload in progress; "
                            "the private snapshot was retained and publication was not submitted"
                        ),
                    )
            elif now >= start_deadline:
                lifecycle.mark_upload_failed()
                return self._result(
                    lifecycle,
                    self.contract.platform,
                    SocialPublicationState.FAILED,
                    (
                        "platform did not acknowledge the selected video; "
                        "publication was not submitted and this stage is safe to retry"
                    ),
                )
            time.sleep(self._poll_interval_seconds)

    def _observe_upload(self, page: BrowserPage, video_path: Path) -> UploadObservation:
        expression = r"""
(() => {
  const marker = '__SPEAK_OUT_UPLOAD_PROBE__';
  const expectedName = %s;
  const expectedPrefix = expectedName.slice(0, 8);
  const progressTexts = %s;
  const failedTexts = %s;
  const completeTexts = %s;
  const body = document.body?.innerText || '';
  const normalized = body.replace(/\s+/g, ' ').trim();
  const fileNames = [...document.querySelectorAll('input[type=file]')]
    .flatMap(input => input.files ? [...input.files].map(file => file.name) : []);
  const expectedFileSeen = normalized.includes(expectedName)
    || (expectedPrefix.length >= 8 && normalized.includes(expectedPrefix))
    || fileNames.includes(expectedName);
  const percentages = [...normalized.matchAll(/(\d{1,3}(?:\.\d+)?)\s*%%/g)]
    .map(match => Number(match[1])).filter(Number.isFinite);
  const inProgress = progressTexts.find(text => normalized.includes(text));
  if (inProgress) {
    if (percentages.some(value => value >= 100)) {
      return {
        state: 'completed',
        evidence: 'platform progress reached 100%%',
        expectedFileSeen,
        remoteConfirmed: true
      };
    }
    const percent = percentages.length ? Math.max(...percentages) : null;
    return {
      state: 'uploading',
      evidence: percent === null ? inProgress : `${inProgress} ${percent}%%`,
      expectedFileSeen
    };
  }
  const failed = failedTexts.find(text => normalized.includes(text));
  if (failed) return {state: 'failed', evidence: failed, expectedFileSeen};
  const complete = completeTexts.find(text => normalized.includes(text));
  if (complete) {
    return {state: 'completed', evidence: complete, expectedFileSeen, remoteConfirmed: true};
  }
  return {state: 'not_started', evidence: marker, expectedFileSeen};
})()
""".strip() % (
            json.dumps(video_path.name),
            json.dumps(list(self.contract.upload_in_progress_texts), ensure_ascii=False),
            json.dumps(list(self.contract.upload_failed_texts), ensure_ascii=False),
            json.dumps(list(self.contract.upload_complete_texts), ensure_ascii=False),
        )
        raw = page.evaluate(expression)
        if not isinstance(raw, dict):
            return UploadObservation(
                state=SocialUploadState.NOT_STARTED,
                evidence="upload probe returned no structured state",
            )
        try:
            state = SocialUploadState(str(raw.get("state", "not_started")))
        except ValueError:
            state = SocialUploadState.NOT_STARTED
        evidence = str(raw.get("evidence") or "platform returned no upload evidence")
        expected_file_seen = bool(
            raw.get("expectedFileSeen", raw.get("expected_file_seen", False))
        )
        remote_confirmed = bool(
            raw.get("remoteConfirmed", raw.get("remote_confirmed", False))
        )
        return UploadObservation(
            state=state,
            evidence=evidence,
            expected_file_seen=expected_file_seen,
            remote_confirmed=remote_confirmed,
        )

    @staticmethod
    def _result(
        lifecycle: BrowserPublishLifecycle,
        platform: SocialPlatform,
        state: SocialPublicationState,
        message: str,
        *,
        permalink: str | None = None,
    ) -> SocialPublishResult:
        return SocialPublishResult(
            platform=platform,
            state=state,
            message=message,
            permalink=permalink,
            upload_state=lifecycle.upload_state,
            submission_started=lifecycle.submission_started,
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
    def _has_click_text(page: BrowserPage, texts: Sequence[str]) -> bool:
        return bool(
            page.evaluate(
                """
(() => {
  const texts = %s;
  const candidates = [...document.querySelectorAll('button,[role=button]')];
  return candidates.some(node => {
    const rect = node.getBoundingClientRect();
    const disabled = node.disabled || node.getAttribute('aria-disabled') === 'true';
    return !disabled && rect.width > 0 && rect.height > 0
      && texts.some(text => node.textContent.trim() === text);
  });
})()
""".strip()
                % json.dumps(list(texts), ensure_ascii=False)
            )
        )

    @staticmethod
    def _click_text(page: BrowserPage, texts: Sequence[str]) -> bool:
        return bool(
            page.evaluate(
                """
(() => {
  const texts = %s;
  const candidates = [...document.querySelectorAll('button,[role=button]')];
  const button = candidates.find(node => {
    const rect = node.getBoundingClientRect();
    const disabled = node.disabled || node.getAttribute('aria-disabled') === 'true';
    return !disabled && rect.width > 0 && rect.height > 0
      && texts.some(text => node.textContent.trim() === text);
  });
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


class XiaohongshuPublisher(_VisibleChromePublisher):
    """Visible-Chrome Adapter for Xiaohongshu creator publication."""

    contract = CONTRACTS[SocialPlatform.XIAOHONGSHU]


class DouyinPublisher(_VisibleChromePublisher):
    """Visible-Chrome Adapter for Douyin creator publication."""

    contract = CONTRACTS[SocialPlatform.DOUYIN]


class BilibiliPublisher(_VisibleChromePublisher):
    """Visible-Chrome Adapter for Bilibili creator publication."""

    contract = CONTRACTS[SocialPlatform.BILIBILI]

    def _fill_platform_metadata(
        self,
        page: BrowserPage,
        spec: SocialPostSpec,
        body_input: str,
        lifecycle: BrowserPublishLifecycle,
    ) -> SocialPublishResult | None:
        if not self._fill_and_verify(page, body_input, spec.body.strip()):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "approved body could not be read back exactly; publication was not submitted",
            )
        tag_input = self._wait_for_any(
            page,
            self.contract.tag_inputs,
            self._editor_timeout_seconds,
        )
        if tag_input is None:
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "approved bilibili tags were not submitted because the tag control is missing",
            )
        if not page.fill_tags(tag_input, spec.tags, self.contract.tag_chips):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                (
                    "approved bilibili tags could not be verified in the tag control; "
                    "publication was not submitted"
                ),
            )
        if spec.category and not self._select_category(page, spec.category):
            return self._result(
                lifecycle,
                spec.platform,
                SocialPublicationState.FAILED,
                "approved bilibili category could not be selected; publication was not submitted",
            )
        return None


_PLATFORM_PUBLISHERS: dict[SocialPlatform, type[_VisibleChromePublisher]] = {
    SocialPlatform.XIAOHONGSHU: XiaohongshuPublisher,
    SocialPlatform.DOUYIN: DouyinPublisher,
    SocialPlatform.BILIBILI: BilibiliPublisher,
}


def create_visible_chrome_publisher(
    platform: SocialPlatform,
    confirmation_timeout_seconds: float = 60,
    editor_timeout_seconds: float = 120,
    upload_start_timeout_seconds: float = 30,
    upload_timeout_seconds: float = 4 * 60 * 60,
    poll_interval_seconds: float = 0.5,
) -> _VisibleChromePublisher:
    """Construct the concrete platform Adapter behind the publisher seam."""

    return _PLATFORM_PUBLISHERS[platform](
        confirmation_timeout_seconds=confirmation_timeout_seconds,
        editor_timeout_seconds=editor_timeout_seconds,
        upload_start_timeout_seconds=upload_start_timeout_seconds,
        upload_timeout_seconds=upload_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
