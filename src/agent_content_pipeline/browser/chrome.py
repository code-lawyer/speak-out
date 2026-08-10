from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field


class RunningProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...


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


class LocalChromeCdpDriver:
    """Launch installed Chrome visibly with a per-platform profile and CDP."""

    def __init__(
        self,
        *,
        project_root: Path,
        chrome_path: Path | None = None,
        process_launcher: Callable[[Sequence[str]], RunningProcess] | None = None,
        http_client: httpx.Client | None = None,
        startup_timeout_seconds: float = 20,
    ) -> None:
        self.project_root = project_root.resolve()
        self.chrome_path = chrome_path or self.discover_chrome()
        self._process_launcher = process_launcher or _launch_visible_chrome
        self._http_client = http_client or httpx.Client()
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
        while time.monotonic() < deadline:
            if active_port_path.is_file():
                lines = active_port_path.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].isdigit():
                    port = int(lines[0])
                    version = self._http_client.get(
                        f"http://127.0.0.1:{port}/json/version",
                        timeout=2,
                    )
                    version.raise_for_status()
                    websocket_url = version.json().get("webSocketDebuggerUrl")
                    if websocket_url:
                        return ChromeSession(
                            platform=platform,
                            profile_root=profile_root,
                            port=port,
                            websocket_url=websocket_url,
                            process_id=process.pid,
                        )
            time.sleep(0.1)
        exit_code = process.poll()
        raise ChromeLaunchError(
            f"Chrome did not expose DevToolsActivePort within {self._startup_timeout_seconds:g}s"
            + (f"; launcher exit code: {exit_code}" if exit_code is not None else "")
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
            websocket_url = version.json().get("webSocketDebuggerUrl")
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
