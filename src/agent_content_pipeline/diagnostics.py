from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from .browser.chrome import MINIMUM_CHROME_MAJOR, LocalChromeCdpDriver
from .config import LocalConfig


def _git_ignores_local(project_root: Path) -> bool:
    resolved_root = project_root.resolve()
    result = subprocess.run(
        (
            "git",
            "-c",
            f"safe.directory={resolved_root.as_posix()}",
            "-C",
            str(resolved_root),
            "check-ignore",
            "--quiet",
            "--",
            ".local/",
        ),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


class DoctorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    detail: str


class DoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    checks: tuple[DoctorCheck, ...]


class SystemDoctor:
    def __init__(
        self,
        *,
        project_root: Path,
        which: Callable[[str], str | None] = shutil.which,
        chrome_discover: Callable[[], Path] = LocalChromeCdpDriver.discover_chrome,
        git_ignore_check: Callable[[Path], bool] = _git_ignores_local,
        chrome_version_read: Callable[[Path], int] = LocalChromeCdpDriver.read_installed_major,
    ) -> None:
        self.project_root = project_root
        self._which = which
        self._chrome_discover = chrome_discover
        self._git_ignore_check = git_ignore_check
        self._chrome_version_read = chrome_version_read

    def run(self) -> DoctorReport:
        checks: list[DoctorCheck] = []
        supported_python = (3, 11) <= sys.version_info[:2] < (3, 13)
        checks.append(
            DoctorCheck(
                name="python",
                ok=supported_python,
                detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            )
        )
        for binary in ("ffmpeg", "ffprobe"):
            resolved = self._which(binary)
            checks.append(
                DoctorCheck(
                    name=binary,
                    ok=bool(resolved and Path(resolved).is_file()),
                    detail=str(resolved) if resolved else "not found on PATH",
                )
            )

        try:
            settings = LocalConfig(self.project_root).load()
            checks.append(DoctorCheck(name="config", ok=True, detail="loaded and validated"))
        except Exception as error:
            settings = None
            checks.append(DoctorCheck(name="config", ok=False, detail=type(error).__name__))

        try:
            local_ignored = self._git_ignore_check(self.project_root)
            ignore_detail = (
                ".local/ is ignored by Git"
                if local_ignored
                else ".local/ is not ignored by Git; add it to .gitignore before storing secrets"
            )
        except Exception as error:
            local_ignored = False
            ignore_detail = f"could not verify .local/ Git ignore status: {type(error).__name__}"
        checks.append(DoctorCheck(name="git-ignore", ok=local_ignored, detail=ignore_detail))

        try:
            configured = settings.browser.chrome_path if settings else ""
            chrome = Path(configured) if configured else self._chrome_discover()
            major = self._chrome_version_read(chrome) if chrome.is_file() else 0
            chrome_ok = chrome.is_file() and major >= MINIMUM_CHROME_MAJOR
            detail = (
                f"{chrome} (Chrome {major})"
                if chrome_ok
                else f"{chrome} reports Chrome {major}; requires Chrome {MINIMUM_CHROME_MAJOR} or newer"
            )
        except Exception as error:
            chrome_ok = False
            detail = str(error)
        checks.append(DoctorCheck(name="chrome", ok=chrome_ok, detail=detail))

        return DoctorReport(ok=all(check.ok for check in checks), checks=tuple(checks))
