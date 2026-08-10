from pathlib import Path

from agent_content_pipeline.browser.cdp import ChromePageController


class FakeCdp:
    def __init__(self):
        self.calls = []

    def command(self, method, params=None, session_id=None):
        self.calls.append((method, params or {}, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "page-1", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 7}
        if method == "Runtime.evaluate":
            return {"result": {"value": True}}
        return {}


def test_page_controller_uses_cdp_for_visible_form_and_file_interactions(tmp_path):
    cdp = FakeCdp()
    page = ChromePageController.attach(cdp)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"video")

    page.navigate("https://example.com/publish")
    assert page.exists("input[type=file]") is True
    page.set_files("input[type=file]", [video])
    page.fill("input[name=title]", "测试标题")
    page.click("button.submit")

    methods = [item[0] for item in cdp.calls]
    assert "Page.navigate" in methods
    assert "DOM.setFileInputFiles" in methods
    assert methods.count("Runtime.evaluate") == 3
    set_files = next(item for item in cdp.calls if item[0] == "DOM.setFileInputFiles")
    assert set_files[1]["files"] == [str(video.resolve())]
    assert all(item[2] == "session-1" for item in cdp.calls[2:])
