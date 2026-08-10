from pathlib import Path

import httpx
import pytest

from agent_content_pipeline.browser.chrome import ChromeLaunchError, LocalChromeCdpDriver


class FakeProcess:
    pid = 1234

    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


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
            json={
                "Browser": "Chrome/140.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:53123/devtools/browser/abc",
            },
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


def test_chrome_driver_rejects_unsupported_browser_before_returning_session(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"exe")

    process = FakeProcess()

    def launch(command):
        profile_arg = next(item for item in command if item.startswith("--user-data-dir="))
        profile = Path(profile_arg.split("=", 1)[1])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").write_text("53123\n/devtools/browser/abc\n")
        return process

    def http(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Browser": "Chrome/115.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:53123/devtools/browser/abc",
            },
            request=request,
        )

    driver = LocalChromeCdpDriver(
        project_root=tmp_path,
        chrome_path=chrome,
        process_launcher=launch,
        http_client=httpx.Client(transport=httpx.MockTransport(http)),
    )

    with pytest.raises(ChromeLaunchError, match="requires Chrome 116 or newer"):
        driver.launch(platform="bilibili", start_url="https://example.com/upload")

    assert process.terminated is True


def test_chrome_driver_retries_transient_cdp_probe_until_browser_is_ready(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"exe")
    process = FakeProcess()
    attempts = 0

    def launch(command):
        profile_arg = next(item for item in command if item.startswith("--user-data-dir="))
        profile = Path(profile_arg.split("=", 1)[1])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").write_text("53123\n/devtools/browser/abc\n")
        return process

    def http(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("not ready", request=request)
        return httpx.Response(
            200,
            json={
                "Browser": "Chrome/140.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:53123/devtools/browser/abc",
            },
            request=request,
        )

    session = LocalChromeCdpDriver(
        project_root=tmp_path,
        chrome_path=chrome,
        process_launcher=launch,
        http_client=httpx.Client(transport=httpx.MockTransport(http)),
        startup_timeout_seconds=1,
    ).launch(platform="bilibili", start_url="https://example.com/upload")

    assert session.port == 53123
    assert attempts == 2
    assert process.terminated is False


def test_chrome_driver_closes_only_the_requested_dedicated_session(tmp_path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"exe")
    calls: list[str] = []

    class FakeBrowserClient:
        def command(self, method, params=None, session_id=None):
            calls.append(method)
            return {}

        def close(self):
            calls.append("transport.close")

    driver = LocalChromeCdpDriver(
        project_root=tmp_path,
        chrome_path=chrome,
        browser_client_factory=lambda _url: FakeBrowserClient(),
    )
    from agent_content_pipeline.browser.chrome import ChromeSession

    target = ChromeSession(
        platform="bilibili",
        profile_root=tmp_path / ".local" / "browser-profiles" / "bilibili",
        port=53123,
        websocket_url="ws://127.0.0.1:53123/devtools/browser/abc",
    )

    driver.close(target)

    assert calls == ["Browser.close", "transport.close"]
