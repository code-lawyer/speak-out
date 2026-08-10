from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .renderer import CommandRunner, SubprocessCommandRunner


class NarrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    root: Path
    audio_path: Path
    subtitles_path: Path
    voice: str


class NarrationError(RuntimeError):
    pass


class EdgeTtsNarrator:
    """Generate narration and SRT through the installed edge-tts module.

    File-based input avoids Windows command-line length and quoting limits. The
    bounded retry/timeout behavior follows lessons from MoneyPrinterTurbo's
    MIT-licensed Edge TTS adapter.
    """

    def __init__(
        self,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 120,
        attempts: int = 3,
    ) -> None:
        self._runner = runner or SubprocessCommandRunner()
        self._timeout_seconds = timeout_seconds
        self._attempts = attempts

    def synthesize(
        self,
        *,
        narration: str,
        voice: str,
        output_root: Path,
        rate: str = "+0%",
    ) -> NarrationResult:
        text = narration.strip()
        if not text:
            raise NarrationError("video narration is empty")
        output_root.mkdir(parents=True, exist_ok=True)
        script_path = output_root / "script.txt"
        audio_path = output_root / "narration.mp3"
        subtitles_path = output_root / "subtitles.srt"
        script_path.write_text(text + "\n", encoding="utf-8")
        command = (
            sys.executable,
            "-m",
            "edge_tts",
            "--file",
            script_path.name,
            "--voice",
            voice,
            f"--rate={rate}",
            "--write-media",
            audio_path.name,
            "--write-subtitles",
            subtitles_path.name,
        )
        last_error: Exception | None = None
        for _ in range(self._attempts):
            try:
                self._runner.run(
                    command,
                    cwd=output_root,
                    timeout_seconds=self._timeout_seconds,
                )
                if audio_path.stat().st_size <= 0:
                    raise NarrationError("Edge TTS produced an empty audio file")
                if not subtitles_path.read_text(encoding="utf-8").strip():
                    raise NarrationError("Edge TTS produced empty subtitles")
                return NarrationResult(
                    root=output_root,
                    audio_path=audio_path,
                    subtitles_path=subtitles_path,
                    voice=voice,
                )
            except (OSError, subprocess.SubprocessError, NarrationError) as error:
                last_error = error
                for path in (audio_path, subtitles_path):
                    if path.is_file() and path.stat().st_size == 0:
                        path.unlink()
        raise NarrationError(
            f"Edge TTS failed after {self._attempts} attempts: {last_error}"
        ) from last_error
