from pathlib import Path

import pytest

from agent_content_pipeline.social.browser_publishers import (
    BrowserPublishLifecycle,
    CONTRACTS,
    create_visible_chrome_publisher as VisibleChromePlatformPublisher,
)
from agent_content_pipeline.social.models import (
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
    SocialUploadState,
)


class FakePage:
    def __init__(
        self,
        *,
        logged_in: bool,
        confirms: bool = True,
        upload_observations=None,
    ):
        self.logged_in = logged_in
        self.confirms = confirms
        self.filled = []
        self.files = []
        self.navigated = None
        self.submit_clicked = False
        self.tags = []
        self.values = {}
        self.upload_observations = list(
            upload_observations
            or [
                {
                    "state": "completed",
                    "evidence": "platform upload completed",
                    "expectedFileSeen": True,
                    "remoteConfirmed": True,
                }
            ]
        )
        self.last_upload_observation = None

    def navigate(self, url):
        self.navigated = url

    def exists(self, selector):
        if "login" in selector or "qrcode" in selector:
            return not self.logged_in
        if (
            "file" in selector
            or "标题" in selector
            or "正文" in selector
            or "描述" in selector
            or "简介" in selector
            or "tag" in selector
            or "标签" in selector
            or "editor" in selector
            or "ProseMirror" in selector
            or "textbox" in selector
            or "partition" in selector
            or "combobox" in selector
            or "select" in selector
        ):
            return self.logged_in
        return False

    def fill(self, selector, value):
        self.filled.append((selector, value))
        self.values[selector] = value

    def read_value(self, selector):
        return self.values.get(selector)

    def set_files(self, selector, paths):
        self.files.extend(paths)

    def fill_tags(self, selector, tags, chip_selectors):
        assert chip_selectors
        self.tags.extend(tags)
        return True

    def evaluate(self, expression):
        if "__SPEAK_OUT_UPLOAD_PROBE__" in expression:
            if len(self.upload_observations) > 1:
                observation = self.upload_observations.pop(0)
            else:
                observation = self.upload_observations[0]
            self.last_upload_observation = observation
            return observation
        if "candidates.some" in expression:
            return True
        if "button.click" in expression:
            self.submit_clicked = True
            return True
        if "control.click" in expression or "option.click" in expression:
            return True
        if "selectedValues" in expression:
            return True
        if "document.body.innerText" in expression:
            return self.confirms
        if "wanted" in expression:
            return True
        return False


def post(tmp_path, platform):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")
    return SocialPostSpec(
        platform=platform,
        title="测试标题",
        body="测试正文",
        tags=["AI"],
        video_path=video,
        category="知识" if platform == SocialPlatform.BILIBILI else None,
    )


def test_publisher_waits_for_platform_upload_completion_before_filling_metadata(
    tmp_path,
):
    class UploadingPage(FakePage):
        def fill(self, selector, value):
            assert self.last_upload_observation == {
                "state": "completed",
                "evidence": "remote video is ready",
                "expectedFileSeen": True,
                "remoteConfirmed": True,
            }
            super().fill(selector, value)

    page = UploadingPage(
        logged_in=True,
        upload_observations=[
            {"state": "uploading", "evidence": "上传中 0%", "expectedFileSeen": True},
            {
                "state": "uploading",
                "evidence": "已上传 1.0MB/164.1MB",
                "expectedFileSeen": True,
            },
            {
                "state": "completed",
                "evidence": "remote video is ready",
                "expectedFileSeen": True,
                "remoteConfirmed": True,
            },
        ],
    )
    result = VisibleChromePlatformPublisher(
        SocialPlatform.DOUYIN,
        confirmation_timeout_seconds=0.01,
        editor_timeout_seconds=0.01,
    ).publish(page, post(tmp_path, SocialPlatform.DOUYIN))

    assert result.state == SocialPublicationState.SUBMITTED


def test_remote_completion_remains_bound_after_platform_hides_the_uploaded_filename(
    tmp_path,
):
    page = FakePage(
        logged_in=True,
        upload_observations=[
            {"state": "not_started", "evidence": "upload page is ready"},
            {"state": "uploading", "evidence": "已上传：99%", "expectedFileSeen": True},
            {
                "state": "completed",
                "evidence": "上传成功",
                "expectedFileSeen": False,
                "remoteConfirmed": True,
            },
        ],
    )

    result = VisibleChromePlatformPublisher(
        SocialPlatform.DOUYIN,
        confirmation_timeout_seconds=0.01,
        editor_timeout_seconds=0.01,
        upload_start_timeout_seconds=0.01,
    ).publish(page, post(tmp_path, SocialPlatform.DOUYIN))

    assert result.state == SocialPublicationState.SUBMITTED


def test_selected_file_is_retained_when_browser_disconnects_before_first_upload_probe(
    tmp_path,
):
    class DisconnectingPage(FakePage):
        def set_files(self, selector, paths):
            super().set_files(selector, paths)
            raise RuntimeError("CDP disconnected after selecting the file")

    lifecycle = BrowserPublishLifecycle()
    page = DisconnectingPage(logged_in=True)

    with pytest.raises(RuntimeError, match="CDP disconnected"):
        VisibleChromePlatformPublisher(SocialPlatform.XIAOHONGSHU).publish(
            page,
            post(tmp_path, SocialPlatform.XIAOHONGSHU),
            lifecycle=lifecycle,
        )

    assert lifecycle.upload_state == SocialUploadState.UPLOADING


def test_local_preview_or_editor_readiness_never_counts_as_remote_upload_completion(
    tmp_path,
):
    page = FakePage(
        logged_in=True,
        upload_observations=[
            {
                "state": "completed",
                "evidence": "local video preview and title editor are ready",
                "expectedFileSeen": True,
                "remoteConfirmed": False,
            }
        ],
    )

    result = VisibleChromePlatformPublisher(
        SocialPlatform.BILIBILI,
        upload_start_timeout_seconds=0,
        poll_interval_seconds=0,
    ).publish(page, post(tmp_path, SocialPlatform.BILIBILI))

    assert result.state == SocialPublicationState.FAILED
    assert result.upload_state == SocialUploadState.FAILED
    assert page.filled == []
    assert page.submit_clicked is False


def test_retrying_a_failed_page_waits_for_the_new_upload_to_replace_stale_failure(
    tmp_path,
):
    class FailedCreatorPage(FakePage):
        def evaluate(self, expression):
            if "location.href" in expression:
                return CONTRACTS[SocialPlatform.XIAOHONGSHU].upload_url
            return super().evaluate(expression)

    page = FailedCreatorPage(
        logged_in=True,
        upload_observations=[
            {"state": "failed", "evidence": "上传失败", "expectedFileSeen": True},
            {"state": "failed", "evidence": "上传失败", "expectedFileSeen": True},
            {"state": "uploading", "evidence": "上传中 1%", "expectedFileSeen": True},
            {
                "state": "completed",
                "evidence": "上传完成",
                "expectedFileSeen": True,
                "remoteConfirmed": True,
            },
        ],
    )

    result = VisibleChromePlatformPublisher(
        SocialPlatform.XIAOHONGSHU,
        upload_start_timeout_seconds=0.1,
        poll_interval_seconds=0,
    ).publish(page, post(tmp_path, SocialPlatform.XIAOHONGSHU))

    assert result.state == SocialPublicationState.SUBMITTED
    assert len(page.files) == 1


def test_publisher_returns_before_submit_but_keeps_upload_lifecycle_in_progress(
    tmp_path,
):
    page = FakePage(
        logged_in=True,
        upload_observations=[{"state": "uploading", "evidence": "上传中 1%"}],
    )
    lifecycle = BrowserPublishLifecycle()

    result = VisibleChromePlatformPublisher(
        SocialPlatform.XIAOHONGSHU,
        upload_start_timeout_seconds=0,
        upload_timeout_seconds=0,
        poll_interval_seconds=0,
    ).publish(
        page,
        post(tmp_path, SocialPlatform.XIAOHONGSHU),
        lifecycle=lifecycle,
    )

    assert result.state == SocialPublicationState.WAITING_FOR_USER
    assert result.upload_state == SocialUploadState.UPLOADING
    assert result.submission_started is False
    assert lifecycle.upload_state == SocialUploadState.UPLOADING
    assert page.filled == []
    assert page.submit_clicked is False


def test_publisher_never_marks_submission_started_when_submit_control_is_missing(
    tmp_path,
):
    class MissingSubmitPage(FakePage):
        def evaluate(self, expression):
            if "candidates.some" in expression:
                return False
            return super().evaluate(expression)

    page = MissingSubmitPage(logged_in=True)
    lifecycle = BrowserPublishLifecycle()
    lifecycle.bind_submission_start(
        lambda: (_ for _ in ()).throw(AssertionError("submission must not start"))
    )

    result = VisibleChromePlatformPublisher(SocialPlatform.DOUYIN).publish(
        page,
        post(tmp_path, SocialPlatform.DOUYIN),
        lifecycle=lifecycle,
    )

    assert result.state == SocialPublicationState.WAITING_FOR_USER
    assert result.upload_state == SocialUploadState.COMPLETED
    assert result.submission_started is False


def test_platform_publisher_stops_for_visible_login_without_touching_form(tmp_path):
    page = FakePage(logged_in=False)
    publisher = VisibleChromePlatformPublisher(SocialPlatform.XIAOHONGSHU)

    result = publisher.publish(page, post(tmp_path, SocialPlatform.XIAOHONGSHU))

    assert result.state == SocialPublicationState.WAITING_FOR_USER
    assert "专用 Chrome" in result.message
    assert page.files == []


def test_each_platform_uploads_the_same_video_and_requires_explicit_confirmation(tmp_path):
    for platform in SocialPlatform:
        page = FakePage(logged_in=True)
        publisher = VisibleChromePlatformPublisher(platform, confirmation_timeout_seconds=0.01)
        spec = post(tmp_path, platform)

        result = publisher.publish(page, spec)

        assert result.state == SocialPublicationState.SUBMITTED
        assert page.files == [spec.video_path]
        if platform == SocialPlatform.BILIBILI:
            assert page.tags == ["AI"]
            assert all("#AI" not in value for _, value in page.filled)
        else:
            assert any("#AI" in value for _, value in page.filled)


def test_platform_publisher_never_submits_when_approved_body_control_is_missing(tmp_path):
    class MissingBodyPage(FakePage):
        def exists(self, selector):
            if "contenteditable" in selector or "textarea" in selector:
                return False
            return super().exists(selector)

    page = MissingBodyPage(logged_in=True)
    result = VisibleChromePlatformPublisher(
        SocialPlatform.XIAOHONGSHU,
        editor_timeout_seconds=0.01,
    ).publish(page, post(tmp_path, SocialPlatform.XIAOHONGSHU))

    assert result.state == SocialPublicationState.FAILED
    assert "body" in result.message
    assert page.submit_clicked is False


def test_each_platform_never_submits_when_title_readback_is_rewritten(tmp_path):
    class RewrittenTitlePage(FakePage):
        def read_value(self, selector):
            value = super().read_value(selector)
            if "标题" in selector and value is not None:
                return value[:-1]
            return value

    for platform in SocialPlatform:
        page = RewrittenTitlePage(logged_in=True)
        result = VisibleChromePlatformPublisher(
            platform,
            editor_timeout_seconds=0.01,
        ).publish(page, post(tmp_path, platform))

        assert result.state == SocialPublicationState.FAILED
        assert "title" in result.message
        assert page.submit_clicked is False


def test_xiaohongshu_and_douyin_never_submit_rewritten_body_or_tags(tmp_path):
    class RewrittenBodyPage(FakePage):
        def read_value(self, selector):
            value = super().read_value(selector)
            if ("正文" in selector or "描述" in selector or "contenteditable" in selector) and value:
                return value.replace("#AI", "")
            return value

    for platform in (SocialPlatform.XIAOHONGSHU, SocialPlatform.DOUYIN):
        page = RewrittenBodyPage(logged_in=True)
        result = VisibleChromePlatformPublisher(
            platform,
            editor_timeout_seconds=0.01,
        ).publish(page, post(tmp_path, platform))

        assert result.state == SocialPublicationState.FAILED
        assert "body and tags" in result.message
        assert page.submit_clicked is False


def test_bilibili_never_submits_when_body_readback_is_rewritten(tmp_path):
    class RewrittenBodyPage(FakePage):
        def read_value(self, selector):
            value = super().read_value(selector)
            if ("简介" in selector or "contenteditable" in selector) and value:
                return value + " 未批准内容"
            return value

    page = RewrittenBodyPage(logged_in=True)
    result = VisibleChromePlatformPublisher(
        SocialPlatform.BILIBILI,
        editor_timeout_seconds=0.01,
    ).publish(page, post(tmp_path, SocialPlatform.BILIBILI))

    assert result.state == SocialPublicationState.FAILED
    assert "body" in result.message
    assert page.submit_clicked is False


def test_bilibili_publisher_never_submits_when_approved_category_cannot_be_selected(
    tmp_path,
):
    class MissingCategoryPage(FakePage):
        def evaluate(self, expression):
            if "option.click" in expression:
                return False
            return super().evaluate(expression)

    page = MissingCategoryPage(logged_in=True)
    result = VisibleChromePlatformPublisher(SocialPlatform.BILIBILI).publish(
        page,
        post(tmp_path, SocialPlatform.BILIBILI),
    )

    assert result.state == SocialPublicationState.FAILED
    assert "category" in result.message
    assert page.submit_clicked is False


def test_bilibili_publisher_never_submits_when_selected_category_cannot_be_verified(
    tmp_path,
):
    class UnverifiedCategoryPage(FakePage):
        def evaluate(self, expression):
            if "selectedValues" in expression:
                return False
            return super().evaluate(expression)

    page = UnverifiedCategoryPage(logged_in=True)
    result = VisibleChromePlatformPublisher(
        SocialPlatform.BILIBILI,
        editor_timeout_seconds=0.01,
    ).publish(page, post(tmp_path, SocialPlatform.BILIBILI))

    assert result.state == SocialPublicationState.FAILED
    assert "category" in result.message
    assert page.submit_clicked is False


def test_bilibili_publisher_never_submits_when_tag_chips_cannot_be_verified(tmp_path):
    class RejectedTagsPage(FakePage):
        def fill_tags(self, selector, tags, chip_selectors):
            self.tags.extend(tags)
            return False

    page = RejectedTagsPage(logged_in=True)
    result = VisibleChromePlatformPublisher(SocialPlatform.BILIBILI).publish(
        page,
        post(tmp_path, SocialPlatform.BILIBILI),
    )

    assert result.state == SocialPublicationState.FAILED
    assert "tags" in result.message
    assert page.submit_clicked is False


def test_bilibili_publisher_rejects_extra_unapproved_tag_chips(tmp_path):
    class ExtraChipPage(FakePage):
        def fill_tags(self, selector, tags, chip_selectors):
            self.tags.extend([*tags, "未批准标签"])
            return False

    page = ExtraChipPage(logged_in=True)
    result = VisibleChromePlatformPublisher(SocialPlatform.BILIBILI).publish(
        page,
        post(tmp_path, SocialPlatform.BILIBILI),
    )

    assert result.state == SocialPublicationState.FAILED
    assert "tags" in result.message
    assert page.submit_clicked is False


def test_user_handoff_before_submit_never_instructs_a_manual_publish(tmp_path):
    class MissingSubmitPage(FakePage):
        def evaluate(self, expression):
            if "button.click" in expression or "candidates.some" in expression:
                return False
            return super().evaluate(expression)

    page = MissingSubmitPage(logged_in=True)
    result = VisibleChromePlatformPublisher(SocialPlatform.DOUYIN).publish(
        page,
        post(tmp_path, SocialPlatform.DOUYIN),
    )

    assert result.state == SocialPublicationState.WAITING_FOR_USER
    assert "do not click publish" in result.message
    assert page.submit_clicked is False
