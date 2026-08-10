from pathlib import Path

from agent_content_pipeline.social.browser_publishers import VisibleChromePlatformPublisher
from agent_content_pipeline.social.models import (
    SocialPlatform,
    SocialPostSpec,
    SocialPublicationState,
)


class FakePage:
    def __init__(self, *, logged_in: bool, confirms: bool = True):
        self.logged_in = logged_in
        self.confirms = confirms
        self.filled = []
        self.files = []
        self.navigated = None
        self.submit_clicked = False
        self.tags = []

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
            or "partition" in selector
            or "combobox" in selector
            or "select" in selector
        ):
            return self.logged_in
        return False

    def fill(self, selector, value):
        self.filled.append((selector, value))

    def set_files(self, selector, paths):
        self.files.extend(paths)

    def fill_tags(self, selector, tags, chip_selectors):
        assert chip_selectors
        self.tags.extend(tags)
        return True

    def evaluate(self, expression):
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


def test_user_handoff_before_submit_never_instructs_a_manual_publish(tmp_path):
    class MissingSubmitPage(FakePage):
        def evaluate(self, expression):
            if "button.click" in expression:
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
