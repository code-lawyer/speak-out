import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.workspace import ProductCreateRequest, ProductWorkspace


def test_agent_authored_copy_for_all_three_platforms_is_versioned(tmp_path):
    product = ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="三平台文案",
            slug="social-copy-test",
            created_on=date(2026, 8, 10),
        )
    )
    copy = tmp_path / "copy.json"
    copy.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "platforms": {
                    "xiaohongshu": {"title": "小红书标题", "body": "正文", "tags": ["AI"]},
                    "douyin": {"title": "抖音标题", "body": "正文", "tags": ["AI"]},
                    "bilibili": {
                        "title": "B站标题",
                        "body": "简介",
                        "tags": ["AI"],
                        "category": "知识",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "artifact",
            "add-social-copy",
            "--product",
            str(product.root),
            "--copy",
            str(copy),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["artifact"]["revision"] == "v001"
    saved = product.root / "publish" / "copy" / "v001" / "copy.json"
    assert set(json.loads(saved.read_text("utf-8"))["platforms"]) == {
        "xiaohongshu",
        "douyin",
        "bilibili",
    }
