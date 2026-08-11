from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from ..workspace import ArtifactIntegrityError, ArtifactKind, ProductWorkspace


class MediaCacheStatus(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    root: Path
    files: int = Field(ge=0)
    bytes: int = Field(ge=0)


class MediaCacheImportResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovered: int = Field(ge=0)
    imported: int = Field(ge=0)
    reused: int = Field(ge=0)
    conflicts: int = Field(ge=0)


class ProjectMediaCache:
    """Own one immutable, project-local cache for reusable media bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.pexels_root = self.root / "pexels"

    def status(self) -> MediaCacheStatus:
        files = (
            [
                path
                for path in self.pexels_root.glob("*.mp4")
                if path.is_file() and not path.is_symlink()
            ]
            if self.pexels_root.is_dir()
            else []
        )
        return MediaCacheStatus(
            root=self.root,
            files=len(files),
            bytes=sum(path.stat().st_size for path in files),
        )

    def import_workspace(self, workspace_root: Path) -> MediaCacheImportResult:
        workspace_root = workspace_root.resolve()
        discovered = imported = reused = conflicts = 0
        if not workspace_root.is_dir():
            return MediaCacheImportResult(
                discovered=0,
                imported=0,
                reused=0,
                conflicts=0,
            )
        self.pexels_root.mkdir(parents=True, exist_ok=True)
        workspace = ProductWorkspace(workspace_root)
        for product_root in sorted(workspace_root.iterdir()):
            if not product_root.is_dir() or not (product_root / "product.toml").is_file():
                continue
            try:
                product = workspace.load(product_root)
            except (OSError, ValueError):
                continue
            for revision in workspace.list_revisions(product):
                if revision.kind is not ArtifactKind.VIDEO_RENDER or revision.digest is None:
                    continue
                staging = workspace.create_staging_directory(
                    product,
                    ArtifactKind.VIDEO_RENDER,
                    "cache-import",
                )
                try:
                    workflow_copy = workspace.copy_verified_file(
                        product,
                        ArtifactKind.VIDEO_RENDER,
                        revision.revision,
                        "workflow.json",
                        staging / "workflow.json",
                    )
                    metadata_copy = workspace.copy_verified_file(
                        product,
                        ArtifactKind.VIDEO_RENDER,
                        revision.revision,
                        "materials/materials.json",
                        staging / "materials.json",
                    )
                    try:
                        workflow_record = json.loads(
                            workflow_copy.path.read_text(encoding="utf-8")
                        )
                        records = json.loads(metadata_copy.path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    if (
                        not isinstance(workflow_record, dict)
                        or workflow_record.get("profile") != "landscape-explainer-v1"
                        or workflow_record.get("materialRevision") is not None
                        or not isinstance(records, list)
                    ):
                        continue
                    for record in records:
                        parsed = self._pexels_record(record)
                        if parsed is None:
                            continue
                        asset_id, filename, width, height = parsed
                        try:
                            source_copy = workspace.copy_verified_file(
                                product,
                                ArtifactKind.VIDEO_RENDER,
                                revision.revision,
                                f"materials/{filename}",
                                staging / filename,
                            )
                        except ArtifactIntegrityError:
                            continue
                        if source_copy.path.stat().st_size <= 0:
                            continue
                        discovered += 1
                        target = self.pexels_root / f"{asset_id}-{width}x{height}.mp4"
                        if target.exists() or target.is_symlink():
                            if (
                                target.is_file()
                                and not target.is_symlink()
                                and self._same_content(source_copy.path, target)
                            ):
                                reused += 1
                            else:
                                conflicts += 1
                            continue
                        outcome = self._install_verified_copy(source_copy.path, target)
                        if outcome == "imported":
                            imported += 1
                        elif outcome == "reused":
                            reused += 1
                        else:
                            conflicts += 1
                except ArtifactIntegrityError:
                    continue
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
        return MediaCacheImportResult(
            discovered=discovered,
            imported=imported,
            reused=reused,
            conflicts=conflicts,
        )

    @staticmethod
    def _pexels_record(record: object) -> tuple[str, str, int, int] | None:
        if not isinstance(record, dict) or record.get("provider") != "pexels":
            return None
        asset_id = str(record.get("assetId", ""))
        filename = str(record.get("file", ""))
        source_url = str(record.get("sourceUrl", ""))
        search_term = str(record.get("searchTerm", "")).strip()
        try:
            width = int(record.get("width", 0))
            height = int(record.get("height", 0))
            duration = float(record.get("durationSeconds", 0))
        except (TypeError, ValueError):
            return None
        relative = Path(filename)
        parsed_source = urlparse(source_url)
        if (
            not asset_id.isdigit()
            or filename != f"pexels-{asset_id}.mp4"
            or width <= 0
            or height <= 0
            or duration <= 0
            or not search_term
            or parsed_source.scheme != "https"
            or parsed_source.hostname not in {"pexels.com", "www.pexels.com"}
            or asset_id not in parsed_source.path
            or relative.is_absolute()
            or len(relative.parts) != 1
            or relative.suffix.lower() != ".mp4"
        ):
            return None
        return asset_id, filename, width, height

    @classmethod
    def _install_verified_copy(cls, source: Path, target: Path) -> str:
        """Install without overwriting a cache entry created by another process."""

        created = False
        try:
            target_handle = target.open("xb")
            created = True
            with target_handle, source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
        except FileExistsError:
            if target.is_file() and not target.is_symlink() and cls._same_content(source, target):
                return "reused"
            return "conflict"
        except BaseException:
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return "imported"

    @staticmethod
    def _same_content(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        return ProjectMediaCache._digest(left) == ProjectMediaCache._digest(right)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
