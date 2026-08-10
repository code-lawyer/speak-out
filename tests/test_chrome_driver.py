from pathlib import Path

import httpx

from agent_content_pipeline.browser.chrome import LocalChromeCdpDriver


class FakeProcess:
    pid = 1234

    def poll(self):
        return None


def test_chrome_driver_launches_visible_dedicated_profile_and_discovers_cdp(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"exe")
    captured: list[str] = []

    def launch(command):
        captured.extend(command)
        profile_arg = next(item for item in command if item.startswith("--user-data-dir="))
        profile = Path(profile_arg.split("=", 1)[1])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").write_text("53123\n/devtools/browser/abc\n")
        return FakeProcess()

    def http(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.port == 53123
        return httpx.Response(
            200,
            json={"webSocketDebuggerUrl": "ws://127.0.0.1:53123/devtools/browser/abc"},
            request=request,
        )

    driver = LocalChromeCdpDriver(
        project_root=tmp_path,
        chrome_path=chrome,
        process_launcher=launch,
        http_client=httpx.Client(transport=httpx.MockTransport(http)),
    )
    session = driver.launch(
        platform="bilibili",
        start_url="https://member.bilibili.com/platform/upload/video/frame",
    )

    expected_profile = tmp_path / ".local" / "browser-profiles" / "bilibili"
    assert session.profile_root == expected_profile
    assert session.port == 53123
    assert session.websocket_url.endswith("/devtools/browser/abc")
    assert f"--user-data-dir={expected_profile}" in captured
    assert "--remote-debugging-port=0" in captured
    assert "--remote-debugging-address=127.0.0.1" in captured
    assert "--remote-allow-origins=http://localhost" in captured
    assert captured[-1].startswith("https://member.bilibili.com/")
