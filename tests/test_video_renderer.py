import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_content_pipeline.video.renderer import FfmpegExplainerRenderer


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is required for the renderer integration test",
)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def test_ffmpeg_renderer_outputs_inspected_1080p_h264_with_narration_and_subtitles(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mp4"
    audio = tmp_path / "narration.m4a"
    subtitles = tmp_path / "subtitles.srt"
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#243447:s=640x360:r=30:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "aac",
            str(audio),
        ]
    )
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,800\n这是一次字幕测试\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    result = FfmpegExplainerRenderer().render_from_assets(
        materials=[source],
        narration_audio=audio,
        subtitles=subtitles,
        output_root=Path("render"),
    )

    assert result.video_path.is_file()
    assert result.width == 1920
    assert result.height == 1080
    assert result.video_codec == "h264"
    assert result.has_audio is True
    assert (result.root / "clip-001.mp4").is_file()
    assert (result.root / "concat.txt").is_file()
    manifest = json.loads((result.root / "render.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "landscape-explainer-v1"


def test_renderer_uses_later_source_windows_when_a_material_repeats(tmp_path):
    class RecordingRunner:
        def __init__(self):
            self.commands = []

        def run(self, command, cwd=None, timeout_seconds=None):
            command = list(command)
            self.commands.append(command)
            if command[0] == "ffprobe" and any(
                item.startswith("stream=codec_type") for item in command
            ):
                stdout = json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 1920,
                                "height": 1080,
                            },
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                        "format": {"duration": "11"},
                    }
                )
            elif command[0] == "ffprobe":
                media = Path(command[-1]).name
                stdout = json.dumps(
                    {"format": {"duration": "11" if "narration" in media else "10"}}
                )
            else:
                stdout = ""
            return SimpleNamespace(stdout=stdout)

    source = tmp_path / "source.mp4"
    audio = tmp_path / "narration.mp3"
    subtitles = tmp_path / "subtitles.srt"
    source.write_bytes(b"video")
    audio.write_bytes(b"audio")
    subtitles.write_text("subtitle", encoding="utf-8")
    runner = RecordingRunner()

    result = FfmpegExplainerRenderer(runner=runner).render_from_assets(
        materials=[source],
        narration_audio=audio,
        subtitles=subtitles,
        output_root=tmp_path / "render-offsets",
    )

    clip_commands = [command for command in runner.commands if "-stream_loop" in command]
    assert "-ss" not in clip_commands[0]
    assert clip_commands[1][clip_commands[1].index("-ss") + 1] == "5.000"
    assert "-ss" not in clip_commands[2]
    manifest = json.loads((result.root / "render.json").read_text(encoding="utf-8"))
    assert [clip["sourceOffsetSeconds"] for clip in manifest["clips"]] == [0.0, 5.0, 0.0]
