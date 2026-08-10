from datetime import date

import pytest

from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactIntegrityError,
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


def test_source_notes_are_versioned_and_all_artifact_revisions_are_discoverable(tmp_path):
    workspace = ProductWorkspace(tmp_path)
    product = workspace.create(
        ProductCreateRequest(
            title="来源记录",
            slug="source-notes",
            created_on=date(2026, 8, 10),
        )
    )
    source = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.SOURCE,
            files={"notes.md": "原始想法".encode("utf-8")},
        ),
    )
    article = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={"article.mdx": b"article"},
        ),
    )

    revisions = workspace.list_revisions(product)

    assert source.root == product.root / "source" / "v001"
    assert [(item.kind, item.revision) for item in revisions] == [
        (ArtifactKind.SOURCE, "v001"),
        (ArtifactKind.ARTICLE, "v001"),
    ]
    assert article in revisions


def test_revision_manifest_detects_any_change_after_artifact_creation(tmp_path):
    workspace = ProductWorkspace(tmp_path)
    product = workspace.create(
        ProductCreateRequest(
            title="完整性测试",
            slug="integrity-test",
            created_on=date(2026, 8, 10),
        )
    )
    revision = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.ARTICLE,
            files={"article.mdx": b"approved bytes"},
        ),
    )

    assert (revision.root / ".artifact.json").is_file()
    assert workspace.verify_revision(product, ArtifactKind.ARTICLE, "v001") == revision

    (revision.root / "article.mdx").write_bytes(b"changed after approval")
    with pytest.raises(ArtifactIntegrityError, match="sha256 mismatch"):
        workspace.verify_revision(product, ArtifactKind.ARTICLE, "v001")


def test_verified_file_copy_is_bound_to_manifest_and_independent_of_later_source_changes(
    tmp_path,
):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="视频快照",
            slug="video-snapshot",
            created_on=date(2026, 8, 10),
        )
    )
    staging = product.root / "video" / "work" / "render"
    (staging / "output").mkdir(parents=True)
    (staging / "output" / "final.mp4").write_bytes(b"approved-video")
    revision = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        staging,
    )
    private_copy = workspace.copy_verified_file(
        product,
        ArtifactKind.VIDEO_RENDER,
        "v001",
        "output/final.mp4",
        product.root / "publish" / ".staging" / "private.mp4",
    )

    (revision.root / "output" / "final.mp4").write_bytes(b"changed-later")

    assert private_copy.revision_digest == revision.digest
    assert private_copy.path.read_bytes() == b"approved-video"
