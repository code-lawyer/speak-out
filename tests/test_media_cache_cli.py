import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ProductCreateRequest,
    ProductWorkspace,
)


def sealed_render_with_pexels_material(tmp_path, *, asset_id: str = "4242"):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="缓存测试",
            slug="cache-test",
            created_on=date(2026, 8, 11),
        )
    )
    staging = product.root / "video" / "work" / "render"
    materials = staging / "materials"
    materials.mkdir(parents=True)
    source = materials / f"pexels-{asset_id}.mp4"
    source.write_bytes(b"existing-material")
    staging.joinpath("workflow.json").write_text(
        json.dumps(
            {
                "materialRevision": None,
                "profile": "landscape-explainer-v1",
            }
        ),
        encoding="utf-8",
    )
    materials.joinpath("materials.json").write_text(
        json.dumps(
            [
                {
                    "provider": "pexels",
                    "assetId": asset_id,
                    "file": source.name,
                    "width": 1920,
                    "height": 1080,
                    "durationSeconds": 12,
                    "sourceUrl": f"https://www.pexels.com/video/example-{asset_id}/",
                    "searchTerm": "technology",
                }
            ]
        ),
        encoding="utf-8",
    )
    revision = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        staging,
    )
    return revision.root / "materials" / source.name


def test_cli_imports_existing_product_materials_into_the_shared_cache(tmp_path):
    source = sealed_render_with_pexels_material(tmp_path)

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
    assert cache_file.read_bytes() == source.read_bytes()
    assert not cache_file.samefile(source)
    assert status.exit_code == 0
    assert json.loads(status.stdout) == {
        "ok": True,
        "root": str(tmp_path / ".local" / "media-cache"),
        "files": 1,
        "bytes": len(b"existing-material"),
    }


def test_cli_never_imports_unsealed_or_forged_workspace_materials(tmp_path):
    forged = tmp_path / "workspace" / "not-a-product" / "video" / "renders" / "v001"
    forged.mkdir(parents=True)
    private_video = forged / "private.mp4"
    private_video.write_bytes(b"private-draft")
    forged.joinpath("materials.json").write_text(
        json.dumps(
            [
                {
                    "provider": "pexels",
                    "assetId": "9999",
                    "file": private_video.name,
                    "width": 1920,
                    "height": 1080,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["media-cache", "import-workspace", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["imported"] == 0
    assert not (tmp_path / ".local" / "media-cache" / "pexels" / "9999-1920x1080.mp4").exists()


def test_cli_never_imports_sealed_local_materials_disguised_as_pexels(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="私有素材",
            slug="private-material",
            created_on=date(2026, 8, 11),
        )
    )
    staging = product.root / "video" / "work" / "render"
    materials = staging / "materials"
    materials.mkdir(parents=True)
    materials.joinpath("pexels-9999.mp4").write_bytes(b"private-draft")
    materials.joinpath("materials.json").write_text(
        json.dumps(
            [
                {
                    "provider": "pexels",
                    "assetId": "9999",
                    "file": "pexels-9999.mp4",
                    "width": 1920,
                    "height": 1080,
                    "durationSeconds": 12,
                    "sourceUrl": "https://www.pexels.com/video/fake-9999/",
                    "searchTerm": "private",
                }
            ]
        ),
        encoding="utf-8",
    )
    staging.joinpath("workflow.json").write_text(
        json.dumps(
            {
                "materialRevision": "v001",
                "profile": "landscape-explainer-v1",
            }
        ),
        encoding="utf-8",
    )
    workspace.commit_revision_directory(product, ArtifactKind.VIDEO_RENDER, staging)

    result = CliRunner().invoke(
        app,
        ["media-cache", "import-workspace", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["imported"] == 0
    assert not (tmp_path / ".local" / "media-cache" / "pexels" / "9999-1920x1080.mp4").exists()
