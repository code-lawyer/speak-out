from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


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
        for metadata_path in workspace_root.rglob("materials.json"):
            try:
                records = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict) or record.get("provider") != "pexels":
                    continue
                asset_id = str(record.get("assetId", ""))
                filename = str(record.get("file", ""))
                try:
                    width = int(record.get("width", 0))
                    height = int(record.get("height", 0))
                except (TypeError, ValueError):
                    continue
                relative = Path(filename)
                if (
                    not asset_id.isdigit()
                    or not filename
                    or width <= 0
                    or height <= 0
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    continue
                source = (metadata_path.parent / relative).resolve()
                if (
                    not source.is_relative_to(metadata_path.parent.resolve())
                    or not source.is_file()
                    or source.is_symlink()
                    or source.stat().st_size <= 0
                ):
                    continue
                discovered += 1
                target = self.pexels_root / f"{asset_id}-{width}x{height}.mp4"
                if target.is_file():
                    if self._same_content(source, target):
                        reused += 1
                    else:
                        conflicts += 1
                    continue
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                imported += 1
        return MediaCacheImportResult(
            discovered=discovered,
            imported=imported,
            reused=reused,
            conflicts=conflicts,
        )

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
