import json

from typer.testing import CliRunner

from agent_content_pipeline.cli import app


def test_cli_imports_existing_product_materials_into_the_shared_cache(tmp_path):
    materials = (
        tmp_path
        / "workspace"
        / "2026-08-11-example"
        / "video"
        / "renders"
        / "v001"
        / "materials"
    )
    materials.mkdir(parents=True)
    source = materials / "pexels-4242.mp4"
    source.write_bytes(b"existing-material")
    materials.joinpath("materials.json").write_text(
        json.dumps(
            [
                {
                    "provider": "pexels",
                    "assetId": "4242",
                    "file": source.name,
                    "width": 1920,
                    "height": 1080,
                }
            ]
        ),
        encoding="utf-8",
    )

    imported = CliRunner().invoke(
        app,
        ["media-cache", "import-workspace", "--project-root", str(tmp_path), "--json"],
    )
    status = CliRunner().invoke(
        app,
        ["media-cache", "status", "--project-root", str(tmp_path), "--json"],
    )

    assert imported.exit_code == 0
    assert json.loads(imported.stdout)["imported"] == 1
    cache_file = tmp_path / ".local" / "media-cache" / "pexels" / "4242-1920x1080.mp4"
    assert cache_file.samefile(source)
    assert status.exit_code == 0
    assert json.loads(status.stdout) == {
        "ok": True,
        "root": str(tmp_path / ".local" / "media-cache"),
        "files": 1,
        "bytes": len(b"existing-material"),
    }
