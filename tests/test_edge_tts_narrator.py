import subprocess
from pathlib import Path

from agent_content_pipeline.video.narration import EdgeTtsNarrator


class FakeRunner:
    def __init__(self) -> None:
        self.command: list[str] | None = None
        self.timeout_seconds: float | None = None

    def run(self, command, cwd=None, timeout_seconds=None):
        self.command = list(command)
        self.timeout_seconds = timeout_seconds
        Path(cwd, "narration.mp3").write_bytes(b"audio")
        Path(cwd, "subtitles.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_edge_tts_narrator_uses_a_script_file_and_preserves_audio_and_srt(tmp_path):
    runner = FakeRunner()
    narrator = EdgeTtsNarrator(runner=runner, timeout_seconds=45)

    result = narrator.synthesize(
        narration="技术改变选择。",
        voice="zh-CN-YunxiNeural",
        output_root=tmp_path / "narration",
    )

    assert result.audio_path.read_bytes() == b"audio"
    assert "技术改变选择。" in (result.root / "script.txt").read_text(encoding="utf-8")
    assert runner.command is not None
    assert runner.command[1:3] == ["-m", "edge_tts"]
    assert runner.command[runner.command.index("--voice") + 1] == "zh-CN-YunxiNeural"
    assert runner.command[runner.command.index("--file") + 1] == "script.txt"
    assert runner.timeout_seconds == 45
