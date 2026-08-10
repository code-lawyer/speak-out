from __future__ import annotations

import json
import hashlib
import shutil
import tomllib
from datetime import date
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArtifactKind(StrEnum):
    SOURCE = "source"
    ARTICLE = "article"
    COVER = "cover"
    VIDEO_SCRIPT = "video-script"
    VIDEO_MATERIAL = "video-material"
    VIDEO_RENDER = "video-render"
    SOCIAL_COPY = "social-copy"


class ArtifactIntegrityError(ValueError):
    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


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
    digest: str | None = None


class ProductWorkspace:
    """Create and locate Product directories without exposing path rules."""

    _DIRECTORIES = ("source", "article", "cover", "video", "publish", "logs")
    _ARTIFACT_PATHS = {
        ArtifactKind.SOURCE: Path("source"),
        ArtifactKind.ARTICLE: Path("article"),
        ArtifactKind.COVER: Path("cover"),
        ArtifactKind.VIDEO_SCRIPT: Path("video") / "script",
        ArtifactKind.VIDEO_MATERIAL: Path("video") / "materials",
        ArtifactKind.VIDEO_RENDER: Path("video") / "renders",
        ArtifactKind.SOCIAL_COPY: Path("publish") / "copy",
    }
    _MANIFEST_NAME = ".artifact.json"

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
        return self._seal_revision(request.kind, revision, revision_root)

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
        return self._seal_revision(kind, revision, revision_root)

    def list_revisions(self, product: Product) -> list[ArtifactRevision]:
        revisions: list[ArtifactRevision] = []
        for kind, relative_root in self._ARTIFACT_PATHS.items():
            artifact_root = product.root / relative_root
            if not artifact_root.is_dir():
                continue
            for path in sorted(
                artifact_root.iterdir(),
                key=lambda candidate: candidate.name,
            ):
                if (
                    path.is_dir()
                    and path.name.startswith("v")
                    and path.name[1:].isdigit()
                ):
                    try:
                        artifact = self.verify_revision(product, kind, path.name)
                    except ArtifactIntegrityError:
                        artifact = ArtifactRevision(kind=kind, revision=path.name, root=path)
                    revisions.append(artifact)
        return revisions

    def verify_revision(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
    ) -> ArtifactRevision:
        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        manifest_path = root / self._MANIFEST_NAME
        if not root.is_dir():
            raise ArtifactIntegrityError((f"artifact revision is missing: {kind.value}:{revision}",))
        if not manifest_path.is_file():
            raise ArtifactIntegrityError(
                (f"artifact integrity manifest is missing: {kind.value}:{revision}",)
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ArtifactIntegrityError(
                (f"artifact integrity manifest is invalid: {kind.value}:{revision}",)
            ) from error
        issues: list[str] = []
        if manifest.get("kind") != kind.value:
            issues.append(f"artifact kind mismatch: expected {kind.value}")
        if manifest.get("revision") != revision:
            issues.append(f"artifact revision mismatch: expected {revision}")
        expected_files = {
            item.get("path"): item
            for item in manifest.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        actual_files = {item["path"]: item for item in self._file_records(root)}
        for missing in sorted(set(expected_files) - set(actual_files)):
            issues.append(f"artifact file is missing: {missing}")
        for added in sorted(set(actual_files) - set(expected_files)):
            issues.append(f"artifact contains unsealed file: {added}")
        for name in sorted(set(expected_files) & set(actual_files)):
            expected = expected_files[name]
            actual = actual_files[name]
            if expected.get("bytes") != actual["bytes"]:
                issues.append(f"artifact byte-size mismatch: {name}")
            if expected.get("sha256") != actual["sha256"]:
                issues.append(f"artifact sha256 mismatch: {name}")
        actual_digest = self._records_digest(tuple(actual_files.values()))
        if manifest.get("revisionDigest") != actual_digest:
            issues.append("artifact revision digest mismatch")
        if issues:
            raise ArtifactIntegrityError(tuple(issues))
        return ArtifactRevision(
            kind=kind,
            revision=revision,
            root=root,
            digest=actual_digest,
        )

    def seal_legacy_revision(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
    ) -> ArtifactRevision:
        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        if not root.is_dir() or not any(path.is_file() for path in root.rglob("*")):
            raise ArtifactIntegrityError(
                (f"legacy artifact revision is missing or empty: {kind.value}:{revision}",)
            )
        manifest_path = root / self._MANIFEST_NAME
        if manifest_path.exists():
            return self.verify_revision(product, kind, revision)
        return self._seal_revision(kind, revision, root)

    def _seal_revision(
        self,
        kind: ArtifactKind,
        revision: str,
        root: Path,
    ) -> ArtifactRevision:
        records = self._file_records(root)
        digest = self._records_digest(records)
        manifest = {
            "schemaVersion": 1,
            "kind": kind.value,
            "revision": revision,
            "revisionDigest": digest,
            "files": records,
        }
        (root / self._MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return ArtifactRevision(kind=kind, revision=revision, root=root, digest=digest)

    @classmethod
    def _file_records(cls, root: Path) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path == root / cls._MANIFEST_NAME or not path.is_file():
                continue
            if path.is_symlink():
                raise ArtifactIntegrityError((f"artifact symlink is not allowed: {path}",))
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        return tuple(records)

    @staticmethod
    def _records_digest(records: tuple[dict[str, object], ...]) -> str:
        canonical = json.dumps(
            list(records),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

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
