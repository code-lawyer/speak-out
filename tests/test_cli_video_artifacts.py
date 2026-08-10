import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.workspace import ProductCreateRequest, ProductWorkspace


def test_cli_adds_an_agent_authored_video_script_revision(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="视频脚本测试",
            slug="video-script-test",
            created_on=date(2026, 8, 10),
        )
    )
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "narration": "技术真正改变的，是普通人的选择空间。",
                "materialTerms": ["technology", "people thinking"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-video-script",
            "--product",
            str(product.root),
            "--script",
            str(script),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["artifact"]["kind"] == "video-script"
    assert payload["artifact"]["revision"] == "v001"
    saved = product.root / "video" / "script" / "v001" / "script.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["materialTerms"] == [
        "technology",
        "people thinking",
    ]


def test_cli_rejects_an_empty_or_llm_shaped_video_script(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="视频脚本测试",
            slug="invalid-video-script",
            created_on=date(2026, 8, 10),
        )
    )
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps({"prompt": "请你自己生成", "materialTerms": []}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-video-script",
            "--product",
            str(product.root),
            "--script",
            str(script),
        ],
    )

    assert result.exit_code == 2
    assert "narration" in result.output
    revision_root = product.root / "video" / "script"
    assert not revision_root.exists() or list(revision_root.iterdir()) == []


def test_cli_copies_local_video_material_into_an_immutable_revision(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="本地素材测试",
            slug="local-material-test",
            created_on=date(2026, 8, 10),
        )
    )
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mov"
    first.write_bytes(b"first-material")
    second.write_bytes(b"second-material")

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-video-material",
            "--product",
            str(product.root),
            "--material",
            str(first),
            "--material",
            str(second),
            "--json",
        ],
    )

    assert result.exit_code == 0
    root = product.root / "video" / "materials" / "v001"
    assert (root / "001.mp4").read_bytes() == b"first-material"
    assert (root / "002.mov").read_bytes() == b"second-material"
