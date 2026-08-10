import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline import cli
from agent_content_pipeline.cli import app
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
from agent_content_pipeline.video.narration import NarrationResult
from agent_content_pipeline.video.renderer import VideoRenderResult
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_video_render_requires_approved_script_and_commits_a_new_render_revision(
    tmp_path, monkeypatch
):
    local_dir = tmp_path / ".local"
    local_dir.mkdir()
    (local_dir / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "unused"

[tts]
voice = "zh-CN-YunxiNeural"
request_timeout_seconds = 45
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="视频渲染测试",
            slug="video-render-test",
            created_on=date(2026, 8, 10),
        )
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_SCRIPT,
            files={
                "script.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "narration": "这是一段旁白。",
                        "materialTerms": ["technology"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            },
        ),
    )
    workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_MATERIAL,
            files={"001.mp4": b"material"},
        ),
    )
    ApprovalLedger(product.root).record(ApprovalScope.VIDEO_SCRIPT, "v001")

    class FakeNarrator:
        def __init__(self, timeout_seconds):
            assert timeout_seconds == 45

        def synthesize(self, *, narration, voice, output_root, rate="+0%"):
            assert narration == "这是一段旁白。"
            assert voice == "zh-CN-YunxiNeural"
            output_root.mkdir(parents=True)
            audio = output_root / "narration.mp3"
            subtitles = output_root / "subtitles.srt"
            audio.write_bytes(b"audio")
            subtitles.write_text("subtitle", encoding="utf-8")
            (output_root / "script.txt").write_text(narration, encoding="utf-8")
            return NarrationResult(
                root=output_root,
                audio_path=audio,
                subtitles_path=subtitles,
                voice=voice,
            )

    class FakeRenderer:
        def render_from_assets(
            self, *, materials, narration_audio, subtitles, output_root, bgm=None
        ):
            assert materials[0].name == "001.mp4"
            output_root.mkdir(parents=True)
            video = output_root / "final.mp4"
            video.write_bytes(b"rendered-video")
            (output_root / "render.json").write_text("{}", encoding="utf-8")
            return VideoRenderResult(
                root=output_root,
                video_path=video,
                width=1920,
                height=1080,
                video_codec="h264",
                has_audio=True,
                duration_seconds=10,
            )

    monkeypatch.setattr(cli, "EdgeTtsNarrator", FakeNarrator)
    monkeypatch.setattr(cli, "FfmpegExplainerRenderer", FakeRenderer)
    result = CliRunner().invoke(
        app,
        [
            "video",
            "render",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--script-revision",
            "v001",
            "--material-revision",
            "v001",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["artifact"]["revision"] == "v001"
    final = product.root / "video" / "renders" / "v001" / "output" / "final.mp4"
    assert final.read_bytes() == b"rendered-video"
