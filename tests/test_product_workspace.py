from datetime import date

from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_user_can_create_a_versioned_product_directory(tmp_path):
    workspace = ProductWorkspace(tmp_path)

    product = workspace.create(
        ProductCreateRequest(
            title="技术加速主义的残酷本质",
            slug="technology-acceleration",
            created_on=date(2026, 8, 10),
        )
    )

    assert product.root == tmp_path / "2026-08-10-technology-acceleration"
    assert product.manifest.title == "技术加速主义的残酷本质"
    assert product.manifest.slug == "technology-acceleration"
    assert (product.root / "product.toml").is_file()
    assert {path.name for path in product.root.iterdir() if path.is_dir()} == {
        "source",
        "article",
        "cover",
        "video",
        "publish",
        "logs",
    }


def test_approved_artifact_revisions_are_append_only(tmp_path):
    workspace = ProductWorkspace(tmp_path)
    product = workspace.create(
        ProductCreateRequest(
            title="版本测试",
            slug="revision-test",
            created_on=date(2026, 8, 10),
        )
    )

    first = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={"article.mdx": b"first"},
        ),
    )
    second = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={"article.mdx": b"second"},
        ),
    )

    assert first.revision == "v001"
    assert second.revision == "v002"
    assert (first.root / "article.mdx").read_bytes() == b"first"
    assert (second.root / "article.mdx").read_bytes() == b"second"


def test_completed_large_artifact_directory_is_committed_without_loading_it_into_memory(tmp_path):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="视频目录提交",
            slug="video-directory-commit",
            created_on=date(2026, 8, 10),
        )
    )
    staging = product.root / "video" / "work" / "render-1"
    (staging / "output").mkdir(parents=True)
    (staging / "output" / "final.mp4").write_bytes(b"video")

    revision = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        staging,
    )

    assert revision.revision == "v001"
    assert (revision.root / "output" / "final.mp4").read_bytes() == b"video"
    assert not staging.exists()
