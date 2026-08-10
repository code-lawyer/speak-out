from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field


class CommandRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessCommandRunner:
    def run(
        self,
        command: Sequence[str],
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )


class VideoRenderResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    root: Path
    video_path: Path
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: str
    has_audio: bool
    duration_seconds: float = Field(gt=0)


class VideoRenderError(RuntimeError):
    pass


class FfmpegExplainerRenderer:
    """Render one shared landscape explainer using system FFmpeg.

    The sequencing and material-normalization ideas are adapted from the
    MIT-licensed MoneyPrinterTurbo project, while composition is implemented
    directly with FFmpeg for a smaller local runtime.
    """

    WIDTH = 1920
    HEIGHT = 1080
    CLIP_SECONDS = 5.0

    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        runner: CommandRunner | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._runner = runner or SubprocessCommandRunner()

    def render_from_assets(
        self,
        *,
        materials: Sequence[Path],
        narration_audio: Path,
        subtitles: Path,
        output_root: Path,
        bgm: Path | None = None,
    ) -> VideoRenderResult:
        if not materials:
            raise VideoRenderError("at least one local or downloaded video material is required")
        for path in (*materials, narration_audio, subtitles):
            if not Path(path).is_file():
                raise VideoRenderError(f"required render input is missing: {path}")
        if bgm is not None and not bgm.is_file():
            raise VideoRenderError(f"BGM file is missing: {bgm}")

        output_root.mkdir(parents=True, exist_ok=False)
        local_audio = output_root / f"narration{narration_audio.suffix.lower()}"
        local_subtitles = output_root / "subtitles.srt"
        shutil.copy2(narration_audio, local_audio)
        shutil.copy2(subtitles, local_subtitles)
        local_bgm: Path | None = None
        if bgm is not None:
            local_bgm = output_root / f"bgm{bgm.suffix.lower()}"
            shutil.copy2(bgm, local_bgm)

        duration = self._duration(local_audio)
        clip_count = max(1, math.ceil(duration / self.CLIP_SECONDS))
        clip_paths: list[Path] = []
        for index in range(clip_count):
            source = Path(materials[index % len(materials)]).resolve()
            clip_duration = min(self.CLIP_SECONDS, duration - index * self.CLIP_SECONDS)
            clip_path = output_root / f"clip-{index + 1:03d}.mp4"
            self._runner.run(
                (
                    self.ffmpeg,
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(source),
                    "-t",
                    f"{clip_duration:.3f}",
                    "-an",
                    "-vf",
                    (
                        f"scale={self.WIDTH}:{self.HEIGHT}:"
                        "force_original_aspect_ratio=increase,"
                        f"crop={self.WIDTH}:{self.HEIGHT},setsar=1,fps=30"
                    ),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip_path),
                ),
                cwd=output_root,
            )
            clip_paths.append(clip_path)

        concat_path = output_root / "concat.txt"
        concat_path.write_text(
            "".join(f"file '{path.name}'\n" for path in clip_paths),
            encoding="utf-8",
        )
        video_path = output_root / "final.mp4"
        command = [
            self.ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path.name,
            "-i",
            local_audio.name,
        ]
        if local_bgm is not None:
            command.extend(("-stream_loop", "-1", "-i", local_bgm.name))
        subtitle_filter = (
            "subtitles=subtitles.srt:charenc=UTF-8:"
            "force_style='FontName=Microsoft YaHei,FontSize=18,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=56'"
        )
        command.extend(("-vf", subtitle_filter, "-map", "0:v:0"))
        if local_bgm is None:
            command.extend(("-map", "1:a:0"))
        else:
            command.extend(
                (
                    "-filter_complex",
                    "[1:a]volume=1.0[voice];[2:a]volume=0.12[bgm];"
                    "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                    "-map",
                    "[aout]",
                )
            )
        command.extend(
            (
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                video_path.name,
            )
        )
        self._runner.run(command, cwd=output_root)
        result = self.inspect(video_path)
        manifest = {
            "profile": "landscape-explainer-v1",
            "width": result.width,
            "height": result.height,
            "videoCodec": result.video_codec,
            "hasAudio": result.has_audio,
            "durationSeconds": result.duration_seconds,
            "materials": [str(Path(path).resolve()) for path in materials],
            "bgm": local_bgm.name if local_bgm else None,
        }
        (output_root / "render.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    def inspect(self, video_path: Path) -> VideoRenderResult:
        completed = self._runner.run(
            (
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height:format=duration",
                "-of",
                "json",
                str(video_path),
            )
        )
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        if video_stream is None:
            raise VideoRenderError("rendered file has no video stream")
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec = str(video_stream.get("codec_name", ""))
        duration = float(payload.get("format", {}).get("duration", 0))
        if (width, height) != (self.WIDTH, self.HEIGHT):
            raise VideoRenderError(f"expected 1920x1080 output; received {width}x{height}")
        if codec != "h264":
            raise VideoRenderError(f"expected H.264 output; received {codec or 'unknown'}")
        if not has_audio:
            raise VideoRenderError("rendered file has no audio stream")
        if duration <= 0:
            raise VideoRenderError("rendered file has no measurable duration")
        return VideoRenderResult(
            root=video_path.parent,
            video_path=video_path,
            width=width,
            height=height,
            video_codec=codec,
            has_audio=has_audio,
            duration_seconds=duration,
        )

    def _duration(self, media_path: Path) -> float:
        completed = self._runner.run(
            (
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(media_path),
            )
        )
        duration = float(json.loads(completed.stdout).get("format", {}).get("duration", 0))
        if duration <= 0:
            raise VideoRenderError(f"media has no measurable duration: {media_path}")
        return duration
