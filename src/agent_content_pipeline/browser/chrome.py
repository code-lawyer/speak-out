from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .cdp import CdpClient, CdpWebSocketClient


MINIMUM_CHROME_MAJOR = 116


class RunningProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class ChromeSession(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    platform: str
    profile_root: Path
    port: int = Field(gt=0, le=65535)
    websocket_url: str
    process_id: int | None = None


class ChromeLaunchError(RuntimeError):
    pass


def _launch_visible_chrome(command: Sequence[str]) -> RunningProcess:
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(list(command), creationflags=creation_flags)


def _read_windows_file_major(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class FixedFileInfo(ctypes.Structure):
        _fields_ = (
            ("signature", wintypes.DWORD),
            ("struct_version", wintypes.DWORD),
            ("file_version_ms", wintypes.DWORD),
            ("file_version_ls", wintypes.DWORD),
            ("product_version_ms", wintypes.DWORD),
            ("product_version_ls", wintypes.DWORD),
            ("file_flags_mask", wintypes.DWORD),
            ("file_flags", wintypes.DWORD),
            ("file_os", wintypes.DWORD),
            ("file_type", wintypes.DWORD),
            ("file_subtype", wintypes.DWORD),
            ("file_date_ms", wintypes.DWORD),
            ("file_date_ls", wintypes.DWORD),
        )

    version_api = ctypes.windll.version
    size = version_api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ChromeLaunchError("could not determine installed Chrome version")
    buffer = ctypes.create_string_buffer(size)
    if not version_api.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ChromeLaunchError("could not read installed Chrome version metadata")
    info_pointer = ctypes.c_void_p()
    info_size = wintypes.UINT()
    if not version_api.VerQueryValueW(
        buffer,
        "\\",
        ctypes.byref(info_pointer),
        ctypes.byref(info_size),
    ):
        raise ChromeLaunchError("Chrome version metadata is missing")
    info = ctypes.cast(info_pointer, ctypes.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise ChromeLaunchError("Chrome version metadata has an invalid signature")
    return int(info.file_version_ms >> 16)


class LocalChromeCdpDriver:
    """Launch installed Chrome visibly with a per-platform profile and CDP."""

    def __init__(
        self,
        *,
        project_root: Path,
        chrome_path: Path | None = None,
        process_launcher: Callable[[Sequence[str]], RunningProcess] | None = None,
        http_client: httpx.Client | None = None,
        browser_client_factory: Callable[[str], CdpClient] | None = None,
        startup_timeout_seconds: float = 20,
    ) -> None:
        self.project_root = project_root.resolve()
        self.chrome_path = chrome_path or self.discover_chrome()
        self._process_launcher = process_launcher or _launch_visible_chrome
        self._http_client = http_client or httpx.Client()
        self._browser_client_factory = browser_client_factory or CdpWebSocketClient
        self._startup_timeout_seconds = startup_timeout_seconds
        self._processes: dict[str, RunningProcess] = {}

    @staticmethod
    def discover_chrome() -> Path:
        candidates: list[Path] = []
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env_name)
            if root:
                candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.extend(
            (
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            )
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ChromeLaunchError(
            "Google Chrome was not found; set [browser].chrome_path in .local/secrets.toml"
        )

    @staticmethod
    def read_installed_major(chrome_path: Path) -> int:
        if sys.platform == "win32":
            return _read_windows_file_major(chrome_path)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            (str(chrome_path), "--version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=creation_flags,
        )
        version_text = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"(?:Chrome|Chromium)\s+(\d+)", version_text)
        if result.returncode != 0 or match is None:
            raise ChromeLaunchError("could not determine installed Chrome version")
        return int(match.group(1))

    def launch(self, *, platform: str, start_url: str) -> ChromeSession:
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", platform) is None:
            raise ValueError("platform profile name must be lowercase letters, digits, or hyphens")
        if not self.chrome_path.is_file():
            raise ChromeLaunchError(f"Chrome executable is missing: {self.chrome_path}")
        profile_root = self.project_root / ".local" / "browser-profiles" / platform
        profile_root.mkdir(parents=True, exist_ok=True)
        active_port_path = profile_root / "DevToolsActivePort"
        existing = self._read_active_session(platform, profile_root, active_port_path)
        if existing is not None:
            return existing
        active_port_path.unlink(missing_ok=True)
        command = (
            str(self.chrome_path),
            f"--user-data-dir={profile_root}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://localhost",
            "--no-first-run",
            "--no-default-browser-check",
            start_url,
        )
        process = self._process_launcher(command)
        self._processes[platform] = process
        deadline = time.monotonic() + self._startup_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if active_port_path.is_file():
                    try:
                        lines = active_port_path.read_text(encoding="utf-8").splitlines()
                        if lines and lines[0].isdigit():
                            port = int(lines[0])
                            version = self._http_client.get(
                                f"http://127.0.0.1:{port}/json/version",
                                timeout=2,
                            )
                            version.raise_for_status()
                            version_payload = version.json()
                            self._validate_browser_version(version_payload)
                            websocket_url = version_payload.get("webSocketDebuggerUrl")
                            if websocket_url:
                                return ChromeSession(
                                    platform=platform,
                                    profile_root=profile_root,
                                    port=port,
                                    websocket_url=websocket_url,
                                    process_id=process.pid,
                                )
                    except (OSError, ValueError, httpx.HTTPError):
                        pass
                time.sleep(0.1)
            exit_code = process.poll()
            raise ChromeLaunchError(
                "Chrome did not expose a ready DevTools endpoint within "
                f"{self._startup_timeout_seconds:g}s"
                + (f"; launcher exit code: {exit_code}" if exit_code is not None else "")
            )
        except Exception:
            self._stop_launched_process(platform, process)
            raise

    def connect_existing(self, *, platform: str) -> ChromeSession | None:
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", platform) is None:
            raise ValueError("platform profile name must be lowercase letters, digits, or hyphens")
        profile_root = self.project_root / ".local" / "browser-profiles" / platform
        return self._read_active_session(
            platform,
            profile_root,
            profile_root / "DevToolsActivePort",
        )

    def _read_active_session(
        self,
        platform: str,
        profile_root: Path,
        active_port_path: Path,
    ) -> ChromeSession | None:
        if not active_port_path.is_file():
            return None
        try:
            lines = active_port_path.read_text(encoding="utf-8").splitlines()
            if not lines or not lines[0].isdigit():
                return None
            port = int(lines[0])
            version = self._http_client.get(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1,
            )
            version.raise_for_status()
            version_payload = version.json()
            self._validate_browser_version(version_payload)
            websocket_url = version_payload.get("webSocketDebuggerUrl")
            if not websocket_url:
                return None
            return ChromeSession(
                platform=platform,
                profile_root=profile_root,
                port=port,
                websocket_url=websocket_url,
            )
        except (OSError, ValueError, httpx.HTTPError):
            return None

    @staticmethod
    def _validate_browser_version(version_payload: dict[str, object]) -> None:
        browser = str(version_payload.get("Browser", ""))
        match = re.search(r"(?:Chrome|Chromium)/(\d+)", browser)
        if match is None:
            raise ChromeLaunchError("Chrome DevTools did not report a supported browser version")
        major = int(match.group(1))
        if major < MINIMUM_CHROME_MAJOR:
            raise ChromeLaunchError(
                f"browser is Chrome {major}; this workflow requires Chrome "
                f"{MINIMUM_CHROME_MAJOR} or newer"
            )

    def close(self, session: ChromeSession) -> None:
        expected_root = (
            self.project_root / ".local" / "browser-profiles" / session.platform
        ).resolve()
        if session.profile_root.resolve() != expected_root:
            raise ChromeLaunchError("refusing to close a Chrome session outside the dedicated profile")
        client = self._browser_client_factory(session.websocket_url)
        try:
            client.command("Browser.close")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _stop_launched_process(self, platform: str, process: RunningProcess) -> None:
        self._processes.pop(platform, None)
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
