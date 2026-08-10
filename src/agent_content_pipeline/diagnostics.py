from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict

from .browser.chrome import LocalChromeCdpDriver
from .config import LocalConfig


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
    ) -> None:
        self.project_root = project_root
        self._which = which
        self._chrome_discover = chrome_discover

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
            configured = settings.browser.chrome_path if settings else ""
            chrome = Path(configured) if configured else self._chrome_discover()
            chrome_ok = chrome.is_file()
            detail = str(chrome)
        except Exception as error:
            chrome_ok = False
            detail = str(error)
        checks.append(DoctorCheck(name="chrome", ok=chrome_ok, detail=detail))

        return DoctorReport(ok=all(check.ok for check in checks), checks=tuple(checks))
