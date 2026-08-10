from __future__ import annotations

import json
import shutil
import tomllib
from datetime import date
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArtifactKind(StrEnum):
    ARTICLE = "article"
    COVER = "cover"
    VIDEO_SCRIPT = "video-script"
    VIDEO_MATERIAL = "video-material"
    VIDEO_RENDER = "video-render"
    SOCIAL_COPY = "social-copy"


class ArtifactRevisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ArtifactKind
    files: dict[str, bytes] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def validate_file_names(cls, files: dict[str, bytes]) -> dict[str, bytes]:
        for name in files:
            candidate = Path(name)
            if candidate.name != name or name in {".", ".."}:
                raise ValueError(f"artifact filename must be a single path segment: {name}")
        return files


class ProductCreateRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    created_on: date


class ProductManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    product_id: str
    title: str
    slug: str
    created_on: date


class Product(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    root: Path
    manifest: ProductManifest


class ArtifactRevision(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kind: ArtifactKind
    revision: str = Field(pattern=r"^v[0-9]{3,}$")
    root: Path


class ProductWorkspace:
    """Create and locate Product directories without exposing path rules."""

    _DIRECTORIES = ("source", "article", "cover", "video", "publish", "logs")
    _ARTIFACT_PATHS = {
        ArtifactKind.ARTICLE: Path("article"),
        ArtifactKind.COVER: Path("cover"),
        ArtifactKind.VIDEO_SCRIPT: Path("video") / "script",
        ArtifactKind.VIDEO_MATERIAL: Path("video") / "materials",
        ArtifactKind.VIDEO_RENDER: Path("video") / "renders",
        ArtifactKind.SOCIAL_COPY: Path("publish") / "copy",
    }

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def create(self, request: ProductCreateRequest) -> Product:
        product_root = self.root / f"{request.created_on.isoformat()}-{request.slug}"
        product_root.mkdir(parents=True, exist_ok=False)
        for name in self._DIRECTORIES:
            (product_root / name).mkdir()

        manifest = ProductManifest(
            product_id=str(uuid4()),
            title=request.title,
            slug=request.slug,
            created_on=request.created_on,
        )
        (product_root / "product.toml").write_text(
            self._serialize_manifest(manifest),
            encoding="utf-8",
        )
        return Product(root=product_root, manifest=manifest)

    def load(self, product_root: Path | str) -> Product:
        root = Path(product_root)
        manifest_path = root / "product.toml"
        with manifest_path.open("rb") as handle:
            manifest = ProductManifest.model_validate(tomllib.load(handle))
        return Product(root=root, manifest=manifest)

    def add_revision(
        self,
        product: Product,
        request: ArtifactRevisionRequest,
    ) -> ArtifactRevision:
        artifact_root = product.root / self._ARTIFACT_PATHS[request.kind]
        artifact_root.mkdir(parents=True, exist_ok=True)
        existing_numbers = [
            int(path.name[1:])
            for path in artifact_root.iterdir()
            if path.is_dir() and path.name.startswith("v") and path.name[1:].isdigit()
        ]
        revision = f"v{(max(existing_numbers, default=0) + 1):03d}"
        revision_root = artifact_root / revision
        revision_root.mkdir(exist_ok=False)
        for name, content in request.files.items():
            (revision_root / name).write_bytes(content)
        return ArtifactRevision(
            kind=request.kind,
            revision=revision,
            root=revision_root,
        )

    def commit_revision_directory(
        self,
        product: Product,
        kind: ArtifactKind,
        source: Path,
    ) -> ArtifactRevision:
        """Atomically place a completed directory into the next revision slot."""

        if not source.is_dir() or not any(source.iterdir()):
            raise ValueError("completed artifact directory must be non-empty")
        artifact_root = product.root / self._ARTIFACT_PATHS[kind]
        artifact_root.mkdir(parents=True, exist_ok=True)
        existing_numbers = [
            int(path.name[1:])
            for path in artifact_root.iterdir()
            if path.is_dir() and path.name.startswith("v") and path.name[1:].isdigit()
        ]
        revision = f"v{(max(existing_numbers, default=0) + 1):03d}"
        revision_root = artifact_root / revision
        try:
            source.replace(revision_root)
        except OSError:
            shutil.move(str(source), str(revision_root))
        return ArtifactRevision(kind=kind, revision=revision, root=revision_root)

    @staticmethod
    def _serialize_manifest(manifest: ProductManifest) -> str:
        return "\n".join(
            (
                f"schema_version = {manifest.schema_version}",
                f"product_id = {json.dumps(manifest.product_id, ensure_ascii=False)}",
                f"title = {json.dumps(manifest.title, ensure_ascii=False)}",
                f"slug = {json.dumps(manifest.slug, ensure_ascii=False)}",
                f"created_on = {json.dumps(manifest.created_on.isoformat())}",
                "",
            )
        )
