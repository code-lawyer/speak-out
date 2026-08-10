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

    def navigate(self, url):
        self.navigated = url

    def exists(self, selector):
        if "login" in selector or "qrcode" in selector:
            return not self.logged_in
        if "file" in selector or "标题" in selector or "正文" in selector or "描述" in selector or "简介" in selector:
            return self.logged_in
        return False

    def fill(self, selector, value):
        self.filled.append((selector, value))

    def set_files(self, selector, paths):
        self.files.extend(paths)

    def evaluate(self, expression):
        if "button.click" in expression:
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
        assert any("#AI" in value for _, value in page.filled)
