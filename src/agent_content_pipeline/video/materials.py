from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field


class DownloadedMaterial(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    path: Path
    provider: str = "pexels"
    asset_id: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    source_url: str
    creator: str | None = None
    search_term: str
    cache_hit: bool = False


class MaterialAcquisitionError(RuntimeError):
    pass


class PexelsMaterialSource:
    """Acquire reusable landscape stock clips through the documented Pexels API."""

    def __init__(
        self,
        api_key: str,
        cache_root: Path | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 60,
    ) -> None:
        if not api_key:
            raise ValueError("Pexels API key is required")
        self._api_key = api_key
        self._cache_root = cache_root.resolve() if cache_root is not None else None
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    def acquire(
        self,
        *,
        terms: Sequence[str],
        destination: Path,
        max_files: int,
        minimum_duration: float = 5,
    ) -> list[DownloadedMaterial]:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        destination.mkdir(parents=True, exist_ok=True)
        client = self._http_client or httpx.Client()
        downloads: list[DownloadedMaterial] = []
        queues: list[tuple[str, list[dict[str, Any]]]] = []
        for raw_term in terms:
            term = raw_term.strip()
            if not term:
                continue
            response = client.get(
                "https://api.pexels.com/v1/videos/search",
                params={"query": term, "per_page": 20, "orientation": "landscape"},
                headers={"Authorization": self._api_key},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            queues.append((term, list(response.json().get("videos", []))))

        seen_ids: set[str] = set()
        while len(downloads) < max_files:
            made_progress = False
            for term, videos in queues:
                while videos:
                    video = videos.pop(0)
                    asset_id = str(video.get("id", ""))
                    duration = float(video.get("duration", 0))
                    if (
                        not asset_id.isdigit()
                        or asset_id in seen_ids
                        or duration < minimum_duration
                    ):
                        continue
                    rendition = self._best_landscape_rendition(video.get("video_files", []))
                    if rendition is None:
                        continue
                    output_path = destination / f"pexels-{asset_id}.mp4"
                    cache_hit = self._materialize(
                        client=client,
                        asset_id=asset_id,
                        rendition=rendition,
                        output_path=output_path,
                    )
                    downloads.append(
                        DownloadedMaterial(
                            path=output_path,
                            asset_id=asset_id,
                            width=int(rendition["width"]),
                            height=int(rendition["height"]),
                            duration_seconds=duration,
                            source_url=str(video.get("url", "")),
                            creator=(video.get("user") or {}).get("name"),
                            search_term=term,
                            cache_hit=cache_hit,
                        )
                    )
                    seen_ids.add(asset_id)
                    made_progress = True
                    break
                if len(downloads) >= max_files:
                    break
            if not made_progress:
                break
        self._write_metadata(destination, downloads)
        if not downloads:
            raise MaterialAcquisitionError("Pexels returned no usable landscape video materials")
        return downloads

    def _materialize(
        self,
        *,
        client: httpx.Client,
        asset_id: str,
        rendition: dict[str, Any],
        output_path: Path,
    ) -> bool:
        if self._cache_root is None:
            self._download(client, str(rendition["link"]), output_path, asset_id)
            return False

        cache_directory = self._cache_root / "pexels"
        cache_directory.mkdir(parents=True, exist_ok=True)
        width = int(rendition["width"])
        height = int(rendition["height"])
        cache_path = cache_directory / f"{asset_id}-{width}x{height}.mp4"
        cache_hit = cache_path.is_file() and cache_path.stat().st_size > 0
        if not cache_hit:
            cache_path.unlink(missing_ok=True)
            temporary = cache_directory / f".{cache_path.name}.{uuid4().hex}.part"
            try:
                self._download(client, str(rendition["link"]), temporary, asset_id)
                try:
                    os.link(temporary, cache_path)
                except FileExistsError:
                    pass
                except OSError:
                    if not cache_path.exists():
                        os.replace(temporary, cache_path)
            finally:
                temporary.unlink(missing_ok=True)
        try:
            os.link(cache_path, output_path)
        except OSError:
            shutil.copy2(cache_path, output_path)
        return cache_hit

    def _download(
        self,
        client: httpx.Client,
        url: str,
        output_path: Path,
        asset_id: str,
    ) -> None:
        with client.stream(
            "GET",
            url,
            timeout=self._timeout_seconds,
        ) as media_response:
            media_response.raise_for_status()
            with output_path.open("xb") as output:
                for chunk in media_response.iter_bytes():
                    output.write(chunk)
        if output_path.stat().st_size <= 0:
            output_path.unlink(missing_ok=True)
            raise MaterialAcquisitionError(
                f"Pexels returned an empty file for asset {asset_id}"
            )

    @staticmethod
    def _best_landscape_rendition(video_files: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [
            item
            for item in video_files
            if int(item.get("width") or 0) > int(item.get("height") or 0)
            and item.get("link")
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                abs(int(item.get("width") or 0) - 1920)
                + abs(int(item.get("height") or 0) - 1080),
                -int(item.get("width") or 0) * int(item.get("height") or 0),
            ),
        )

    @staticmethod
    def _write_metadata(destination: Path, downloads: Sequence[DownloadedMaterial]) -> None:
        payload = [
            {
                "provider": item.provider,
                "assetId": item.asset_id,
                "file": item.path.name,
                "width": item.width,
                "height": item.height,
                "durationSeconds": item.duration_seconds,
                "sourceUrl": item.source_url,
                "creator": item.creator,
                "searchTerm": item.search_term,
                "cacheHit": item.cache_hit,
            }
            for item in downloads
        ]
        (destination / "materials.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
