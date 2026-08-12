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


class ArtifactSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ArtifactKind
    revision: str = Field(pattern=r"^v[0-9]{3,}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: dict[str, bytes]


class VerifiedFileCopy(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    path: Path
    revision_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactFileIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)


class VerifiedRevisionCopy(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    root: Path
    revision_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    files: tuple[Path, ...]


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
    _STAGING_PATHS = {
        ArtifactKind.VIDEO_RENDER: Path("video") / "work",
        ArtifactKind.SOCIAL_COPY: Path("publish") / ".staging",
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

    def resolve_artifact_file(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
        relative_path: str,
    ) -> Path:
        """Resolve one Product artifact file without exposing layout rules to callers."""

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact path must stay inside its revision")
        revision_root = product.root / self._ARTIFACT_PATHS[kind] / revision
        candidate = revision_root / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ArtifactIntegrityError(
                (f"artifact file is missing: {kind.value}:{revision}:{relative.as_posix()}",)
            )
        return candidate

    def create_staging_directory(
        self,
        product: Product,
        kind: ArtifactKind,
        purpose: str,
    ) -> Path:
        """Create one private Product-local staging directory for a known artifact area."""

        if kind not in self._STAGING_PATHS:
            raise ValueError(f"artifact kind has no staging area: {kind.value}")
        if not purpose or any(character not in "abcdefghijklmnopqrstuvwxyz-" for character in purpose):
            raise ValueError("staging purpose must contain lowercase letters and hyphens only")
        staging_root = product.root / self._STAGING_PATHS[kind]
        staging_root.mkdir(parents=True, exist_ok=True)
        path = staging_root / f"{purpose}-{uuid4().hex}"
        path.mkdir(exist_ok=False)
        return path

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
        if not root.is_dir():
            raise ArtifactIntegrityError(
                (f"artifact revision is missing: {kind.value}:{revision}",)
            )
        manifest = self._read_integrity_manifest(root, kind, revision)
        actual_records = self._file_records(root)
        actual_digest, issues = self._validate_manifest_records(
            manifest,
            kind,
            revision,
            actual_records,
        )
        if issues:
            raise ArtifactIntegrityError(tuple(issues))
        return ArtifactRevision(
            kind=kind,
            revision=revision,
            root=root,
            digest=actual_digest,
        )

    def read_verified_revision(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
    ) -> ArtifactSnapshot:
        """Read one immutable byte snapshot and verify those same bytes against its manifest."""

        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        if not root.is_dir():
            raise ArtifactIntegrityError(
                (f"artifact revision is missing: {kind.value}:{revision}",)
            )
        manifest = self._read_integrity_manifest(root, kind, revision)
        try:
            files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file() and path.name != self._MANIFEST_NAME
            }
        except (OSError, ValueError) as error:
            raise ArtifactIntegrityError(
                (f"artifact snapshot could not be read: {kind.value}:{revision}",)
            ) from error
        actual_records = tuple(
            {
                "path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in files.items()
        )
        digest, issues = self._validate_manifest_records(
            manifest,
            kind,
            revision,
            actual_records,
        )
        if issues:
            raise ArtifactIntegrityError(tuple(issues))
        return ArtifactSnapshot(
            kind=kind,
            revision=revision,
            digest=digest,
            files=files,
        )

    def copy_verified_file(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
        relative_path: str,
        destination: Path,
    ) -> VerifiedFileCopy:
        """Copy one approved file to a private path while hashing the copied bytes."""

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact copy path must stay inside its revision")
        product_root = product.root.resolve()
        destination = destination.resolve()
        if not destination.is_relative_to(product_root):
            raise ValueError("verified file copies must stay inside the Product directory")
        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        manifest_path = root / self._MANIFEST_NAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ArtifactIntegrityError(
                (f"artifact integrity manifest is invalid: {kind.value}:{revision}",)
            ) from error
        records = tuple(
            item
            for item in manifest.get("files", [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("bytes"), int)
            and isinstance(item.get("sha256"), str)
        )
        manifest_digest = self._records_digest(records)
        expected = next((item for item in records if item["path"] == relative.as_posix()), None)
        issues = []
        if manifest.get("kind") != kind.value:
            issues.append(f"artifact kind mismatch: expected {kind.value}")
        if manifest.get("revision") != revision:
            issues.append(f"artifact revision mismatch: expected {revision}")
        if manifest.get("revisionDigest") != manifest_digest:
            issues.append("artifact revision digest mismatch")
        if expected is None:
            issues.append(f"artifact file is missing from manifest: {relative.as_posix()}")
        if issues:
            raise ArtifactIntegrityError(tuple(issues))
        source = root / relative
        if source.is_symlink():
            raise ArtifactIntegrityError((f"artifact symlink is not allowed: {relative_path}",))
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        copied_bytes = 0
        try:
            with source.open("rb") as source_handle, destination.open("xb") as target_handle:
                while chunk := source_handle.read(1024 * 1024):
                    target_handle.write(chunk)
                    digest.update(chunk)
                    copied_bytes += len(chunk)
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise ArtifactIntegrityError(
                (f"artifact file snapshot failed: {relative.as_posix()}",)
            ) from error
        file_sha256 = digest.hexdigest()
        if copied_bytes != expected["bytes"] or file_sha256 != expected["sha256"]:
            destination.unlink(missing_ok=True)
            raise ArtifactIntegrityError(
                (f"artifact changed while copying: {relative.as_posix()}",)
            )
        return VerifiedFileCopy(
            path=destination,
            revision_digest=manifest_digest,
            file_sha256=file_sha256,
        )

    def artifact_file_identity(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
        relative_path: str,
    ) -> ArtifactFileIdentity:
        """Return one file identity bound to the sealed revision manifest."""

        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact identity path must stay inside its revision")
        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        manifest = self._read_integrity_manifest(root, kind, revision)
        records = self._manifest_records(manifest)
        revision_digest = self._records_digest(records)
        issues: list[str] = []
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or len(records) != len(raw_files):
            issues.append("artifact integrity manifest contains invalid file records")
        if len({str(item["path"]) for item in records}) != len(records):
            issues.append("artifact integrity manifest contains duplicate file paths")
        if manifest.get("kind") != kind.value:
            issues.append(f"artifact kind mismatch: expected {kind.value}")
        if manifest.get("revision") != revision:
            issues.append(f"artifact revision mismatch: expected {revision}")
        if manifest.get("revisionDigest") != revision_digest:
            issues.append("artifact revision digest mismatch")
        record = next((item for item in records if item["path"] == relative.as_posix()), None)
        if record is None:
            issues.append(f"artifact file is missing from manifest: {relative.as_posix()}")
        if issues:
            raise ArtifactIntegrityError(tuple(issues))
        assert record is not None
        return ArtifactFileIdentity(
            revision_digest=revision_digest,
            file_sha256=record["sha256"],
            bytes=record["bytes"],
        )

    def copy_verified_revision(
        self,
        product: Product,
        kind: ArtifactKind,
        revision: str,
        destination: Path,
    ) -> VerifiedRevisionCopy:
        """Stream one sealed revision into a Product-local immutable render snapshot."""

        product_root = product.root.resolve()
        destination = destination.resolve()
        if not destination.is_relative_to(product_root):
            raise ValueError("verified revision copies must stay inside the Product directory")
        root = product.root / self._ARTIFACT_PATHS[kind] / revision
        if not root.is_dir():
            raise ArtifactIntegrityError(
                (f"artifact revision is missing: {kind.value}:{revision}",)
            )
        manifest = self._read_integrity_manifest(root, kind, revision)
        expected_records = self._manifest_records(manifest)
        manifest_digest, manifest_issues = self._validate_manifest_records(
            manifest,
            kind,
            revision,
            expected_records,
        )
        if manifest_issues:
            raise ArtifactIntegrityError(tuple(manifest_issues))
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise ArtifactIntegrityError(
                (f"artifact revision snapshot destination could not be created: {destination}",)
            ) from error

        copied_records: list[dict[str, object]] = []
        copied_paths: list[Path] = []
        try:
            for record in expected_records:
                relative = Path(str(record["path"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ArtifactIntegrityError(
                        (f"artifact manifest path escapes revision: {relative.as_posix()}",)
                    )
                source = root / relative
                if source.is_symlink():
                    raise ArtifactIntegrityError(
                        (f"artifact symlink is not allowed: {relative.as_posix()}",)
                    )
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied_bytes = 0
                with source.open("rb") as source_handle, target.open("xb") as target_handle:
                    while chunk := source_handle.read(1024 * 1024):
                        target_handle.write(chunk)
                        digest.update(chunk)
                        copied_bytes += len(chunk)
                copied = {
                    "path": relative.as_posix(),
                    "bytes": copied_bytes,
                    "sha256": digest.hexdigest(),
                }
                copied_records.append(copied)
                copied_paths.append(target)
                if copied["bytes"] != record["bytes"] or copied["sha256"] != record["sha256"]:
                    raise ArtifactIntegrityError(
                        (f"artifact changed while copying: {relative.as_posix()}",)
                    )
            copied_digest = self._records_digest(tuple(copied_records))
            if copied_digest != manifest_digest:
                raise ArtifactIntegrityError(("artifact revision changed while copying",))
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return VerifiedRevisionCopy(
            root=destination,
            revision_digest=manifest_digest,
            files=tuple(copied_paths),
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

    def _read_integrity_manifest(
        self,
        root: Path,
        kind: ArtifactKind,
        revision: str,
    ) -> dict[str, object]:
        manifest_path = root / self._MANIFEST_NAME
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
        if not isinstance(manifest, dict):
            raise ArtifactIntegrityError(
                (f"artifact integrity manifest is invalid: {kind.value}:{revision}",)
            )
        return manifest

    @staticmethod
    def _manifest_records(manifest: dict[str, object]) -> tuple[dict[str, object], ...]:
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            return ()
        return tuple(
            item
            for item in raw_files
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("bytes"), int)
            and isinstance(item.get("sha256"), str)
        )

    @classmethod
    def _validate_manifest_records(
        cls,
        manifest: dict[str, object],
        kind: ArtifactKind,
        revision: str,
        actual_records: tuple[dict[str, object], ...],
    ) -> tuple[str, list[str]]:
        expected_sequence = cls._manifest_records(manifest)
        expected_files = {str(item["path"]): item for item in expected_sequence}
        actual_files = {str(item["path"]): item for item in actual_records}
        issues: list[str] = []
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list) or len(expected_sequence) != len(raw_files):
            issues.append("artifact integrity manifest contains invalid file records")
        if len(expected_files) != len(expected_sequence):
            issues.append("artifact integrity manifest contains duplicate file paths")
        if manifest.get("kind") != kind.value:
            issues.append(f"artifact kind mismatch: expected {kind.value}")
        if manifest.get("revision") != revision:
            issues.append(f"artifact revision mismatch: expected {revision}")
        for missing in sorted(set(expected_files) - set(actual_files)):
            issues.append(f"artifact file is missing: {missing}")
        for added in sorted(set(actual_files) - set(expected_files)):
            issues.append(f"artifact contains unsealed file: {added}")
        for name in sorted(set(expected_files) & set(actual_files)):
            expected = expected_files[name]
            actual = actual_files[name]
            if expected.get("bytes") != actual.get("bytes"):
                issues.append(f"artifact byte-size mismatch: {name}")
            if expected.get("sha256") != actual.get("sha256"):
                issues.append(f"artifact sha256 mismatch: {name}")
        digest = cls._records_digest(actual_records)
        if manifest.get("revisionDigest") != digest:
            issues.append("artifact revision digest mismatch")
        return digest, issues

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
