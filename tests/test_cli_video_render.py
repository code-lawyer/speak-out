import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_video_render_cli_enforces_explicit_external_data_transfer_gates(tmp_path):
    local_dir = tmp_path / ".local"
    local_dir.mkdir()
    (local_dir / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://hillward.top/api/articles"
bearer_token = "test-only-token"

[tts]
voice = "zh-CN-YunxiNeural"
request_timeout_seconds = 45
""".lstrip(),
        encoding="utf-8",
    )
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="视频渲染授权测试",
            slug="video-render-consent",
            created_on=date(2026, 8, 10),
        )
    )
    script_revision = workspace.add_revision(
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
    ApprovalLedger(product.root).record(
        ApprovalScope.VIDEO_SCRIPT,
        "v001",
        script_revision.digest,
    )

    edge_denied = CliRunner().invoke(
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
        ],
    )
    assert edge_denied.exit_code != 0
    assert "--allow-edge-tts-data-transfer" in edge_denied.output

    pexels_denied = CliRunner().invoke(
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
            "--allow-edge-tts-data-transfer",
        ],
    )
    assert pexels_denied.exit_code != 0
    assert "--allow-pexels-data-transfer" in pexels_denied.output
