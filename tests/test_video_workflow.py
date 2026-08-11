import json
from datetime import date

from agent_content_pipeline.config import LocalSecrets
from agent_content_pipeline.state import ApprovalScope
from agent_content_pipeline.video.narration import NarrationResult
from agent_content_pipeline.video.renderer import VideoRenderResult
from agent_content_pipeline.video.workflow import VideoRenderRequest, VideoRenderWorkflow
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def _settings() -> LocalSecrets:
    return LocalSecrets.model_validate(
        {
            "website_wechat": {
                "endpoint": "https://hillward.top/api/articles",
                "bearer_token": "test-only-token",
            },
            "tts": {
                "voice": "zh-CN-YunxiNeural",
                "request_timeout_seconds": 45,
            },
        }
    )


def test_video_workflow_uses_the_approved_script_snapshot_and_private_material_copy(
    tmp_path,
):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="视频不可变输入",
            slug="video-immutable-inputs",
            created_on=date(2026, 8, 10),
        )
    )
    script = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_SCRIPT,
            files={
                "script.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "narration": "审核通过的旁白。",
                        "materialTerms": ["technology"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            },
        ),
    )
    material = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_MATERIAL,
            files={"001.mp4": b"approved-material"},
        ),
    )

    class MutatingApprovalLedger:
        def has(self, scope, revision, content_digest=None):
            assert scope == ApprovalScope.VIDEO_SCRIPT
            assert revision == "v001"
            assert content_digest == script.digest
            script.root.joinpath("script.json").write_text(
                '{"schemaVersion":1,"narration":"未审核替换","materialTerms":["bad"]}',
                encoding="utf-8",
            )
            return True

    class FakeNarrator:
        def synthesize(self, *, narration, voice, output_root, rate="+0%"):
            assert narration == "审核通过的旁白。"
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

    class MutatingRenderer:
        def render_from_assets(
            self, *, materials, narration_audio, subtitles, output_root, bgm=None
        ):
            material.root.joinpath("001.mp4").write_bytes(b"changed-after-snapshot")
            assert materials[0].read_bytes() == b"approved-material"
            assert materials[0].is_relative_to(product.root / "video" / "work")
            output_root.mkdir(parents=True)
            video = output_root / "final.mp4"
            video.write_bytes(b"rendered-video")
            return VideoRenderResult(
                root=output_root,
                video_path=video,
                width=1920,
                height=1080,
                video_codec="h264",
                has_audio=True,
                duration_seconds=10,
            )

    result = VideoRenderWorkflow(
        workspace=workspace,
        product=product,
        settings=_settings(),
        approval_ledger=MutatingApprovalLedger(),
        narrator=FakeNarrator(),
        renderer=MutatingRenderer(),
    ).render(
        VideoRenderRequest(
            script_revision="v001",
            material_revision="v001",
            allow_edge_tts_data_transfer=True,
        )
    )

    assert result.revision.revision == "v001"
    assert result.final_path.read_bytes() == b"rendered-video"
    assert result.revision.root.joinpath("materials", "001.mp4").read_bytes() == b"approved-material"


def test_video_workflow_snapshots_local_narration_before_rendering(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="本地旁白快照",
            slug="local-narration-snapshot",
            created_on=date(2026, 8, 10),
        )
    )
    script = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.VIDEO_SCRIPT,
            files={
                "script.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "narration": "本地旁白。",
                        "materialTerms": ["local"],
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
    audio = tmp_path / "voice.mp3"
    subtitles = tmp_path / "voice.srt"
    audio.write_bytes(b"local-audio")
    subtitles.write_text("approved subtitles", encoding="utf-8")

    class Approved:
        def has(self, scope, revision, content_digest=None):
            return content_digest == script.digest

    class ForbiddenNarrator:
        def synthesize(self, **kwargs):
            raise AssertionError("online narration must not be called")

    class SnapshotRenderer:
        def render_from_assets(
            self, *, materials, narration_audio, subtitles, output_root, bgm=None
        ):
            audio.write_bytes(b"changed")
            globals_subtitles = tmp_path / "voice.srt"
            globals_subtitles.write_text("changed", encoding="utf-8")
            assert narration_audio.read_bytes() == b"local-audio"
            assert subtitles.read_text(encoding="utf-8") == "approved subtitles"
            output_root.mkdir(parents=True)
            video = output_root / "final.mp4"
            video.write_bytes(b"video")
            return VideoRenderResult(
                root=output_root,
                video_path=video,
                width=1920,
                height=1080,
                video_codec="h264",
                has_audio=True,
                duration_seconds=1,
            )

    result = VideoRenderWorkflow(
        workspace=workspace,
        product=product,
        settings=_settings(),
        approval_ledger=Approved(),
        narrator=ForbiddenNarrator(),
        renderer=SnapshotRenderer(),
    ).render(
        VideoRenderRequest(
            script_revision="v001",
            material_revision="v001",
            narration_audio=audio,
            subtitles=subtitles,
        )
    )

    assert result.final_path.read_bytes() == b"video"
