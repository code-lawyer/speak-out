from __future__ import annotations

import json
import math
import random
import shutil
from pathlib import Path
from typing import Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..config import LocalSecrets
from ..state import ApprovalLedger, ApprovalScope
from ..workspace import ArtifactKind, ArtifactRevision, Product, ProductWorkspace
from .materials import DownloadedMaterial, PexelsMaterialSource
from .narration import EdgeTtsNarrator, NarrationResult
from .renderer import FfmpegExplainerRenderer, VideoRenderResult
from .spec import VideoScriptSpec


class ApprovalReader(Protocol):
    def has(
        self,
        scope: ApprovalScope,
        revision: str,
        content_digest: str | None = None,
    ) -> bool: ...


class Narrator(Protocol):
    def synthesize(
        self,
        *,
        narration: str,
        voice: str,
        output_root: Path,
        rate: str = "+0%",
    ) -> NarrationResult: ...


class MaterialSource(Protocol):
    def acquire(
        self,
        *,
        terms: Sequence[str],
        destination: Path,
        max_files: int,
        minimum_duration: float = 5,
    ) -> list[DownloadedMaterial]: ...


class Renderer(Protocol):
    def render_from_assets(
        self,
        *,
        materials: Sequence[Path],
        narration_audio: Path,
        subtitles: Path,
        output_root: Path,
        bgm: Path | None = None,
    ) -> VideoRenderResult: ...


class VideoWorkflowError(ValueError):
    pass


class VideoRenderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    script_revision: str = Field(pattern=r"^v[0-9]{3,}$")
    material_revision: str | None = Field(default=None, pattern=r"^v[0-9]{3,}$")
    material_count: int | None = Field(default=None, ge=1, le=36)
    bgm_directory: Path | None = None
    narration_audio: Path | None = None
    subtitles: Path | None = None
    allow_edge_tts_data_transfer: bool = False
    allow_pexels_data_transfer: bool = False


class VideoWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    revision: ArtifactRevision
    rendered: VideoRenderResult
    final_path: Path


class VideoRenderWorkflow:
    """Bind approved bytes to every render input and commit one complete revision."""

    _VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
    _AUDIO_SUFFIXES = {".mp3", ".m4a", ".wav", ".aac"}

    def __init__(
        self,
        *,
        workspace: ProductWorkspace,
        product: Product,
        settings: LocalSecrets,
        approval_ledger: ApprovalReader | None = None,
        narrator: Narrator | None = None,
        material_source: MaterialSource | None = None,
        renderer: Renderer | None = None,
    ) -> None:
        self._workspace = workspace
        self._product = product
        self._settings = settings
        self._approval_ledger = approval_ledger or ApprovalLedger(product.root)
        self._narrator = narrator
        self._material_source = material_source
        self._renderer = renderer or FfmpegExplainerRenderer()

    def render(self, request: VideoRenderRequest) -> VideoWorkflowResult:
        script_snapshot = self._workspace.read_verified_revision(
            self._product,
            ArtifactKind.VIDEO_SCRIPT,
            request.script_revision,
        )
        if not self._approval_ledger.has(
            ApprovalScope.VIDEO_SCRIPT,
            request.script_revision,
            script_snapshot.digest,
        ):
            raise VideoWorkflowError(
                f"explicit video-script approval is required for {request.script_revision}"
            )
        script_bytes = script_snapshot.files.get("script.json")
        if script_bytes is None:
            raise VideoWorkflowError("video-script revision is missing script.json")
        spec = VideoScriptSpec.model_validate_json(script_bytes)
        self._validate_request(request)

        staging = self._product.root / "video" / "work" / f"render-{uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            materials = self._prepare_materials(request, spec, staging)
            bgm = self._prepare_bgm(request.bgm_directory, staging)
            narration = self._prepare_narration(request, spec, staging)
            rendered = self._renderer.render_from_assets(
                materials=materials,
                narration_audio=narration.audio_path,
                subtitles=narration.subtitles_path,
                output_root=staging / "output",
                bgm=bgm,
            )
            (staging / "workflow.json").write_text(
                json.dumps(
                    {
                        "scriptRevision": request.script_revision,
                        "scriptDigest": script_snapshot.digest,
                        "materialRevision": request.material_revision,
                        "materialCount": len(materials),
                        "voice": narration.voice,
                        "narrationSource": (
                            "local" if request.narration_audio is not None else "edge-tts"
                        ),
                        "bgm": bgm.relative_to(staging).as_posix() if bgm else None,
                        "profile": "landscape-explainer-v1",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            revision = self._workspace.commit_revision_directory(
                self._product,
                ArtifactKind.VIDEO_RENDER,
                staging,
            )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        final_path = revision.root / "output" / rendered.video_path.name
        return VideoWorkflowResult(
            revision=revision,
            rendered=rendered,
            final_path=final_path,
        )

    def _validate_request(self, request: VideoRenderRequest) -> None:
        if (request.narration_audio is None) != (request.subtitles is None):
            raise VideoWorkflowError(
                "--narration-audio and --subtitles must be supplied together"
            )
        if request.narration_audio is not None:
            if not request.narration_audio.is_file():
                raise VideoWorkflowError(
                    f"local narration audio is missing: {request.narration_audio}"
                )
            assert request.subtitles is not None
            if not request.subtitles.is_file():
                raise VideoWorkflowError(f"local subtitles are missing: {request.subtitles}")
        elif not request.allow_edge_tts_data_transfer:
            raise VideoWorkflowError(
                "Edge TTS sends the approved narration text to Microsoft; explicit "
                "--allow-edge-tts-data-transfer is required, or provide local "
                "--narration-audio and --subtitles"
            )
        if request.material_revision is None and not request.allow_pexels_data_transfer:
            raise VideoWorkflowError(
                "Pexels receives the approved material search terms; explicit "
                "--allow-pexels-data-transfer is required, or provide --material-revision"
            )
        if request.material_revision is not None and request.material_count is not None:
            raise VideoWorkflowError(
                "--material-count applies only to Pexels acquisition; omit it with "
                "--material-revision"
            )

    def _prepare_materials(
        self,
        request: VideoRenderRequest,
        spec: VideoScriptSpec,
        staging: Path,
    ) -> list[Path]:
        if request.material_revision is not None:
            snapshot = self._workspace.copy_verified_revision(
                self._product,
                ArtifactKind.VIDEO_MATERIAL,
                request.material_revision,
                staging / "materials",
            )
            paths = [
                path for path in snapshot.files if path.suffix.lower() in self._VIDEO_SUFFIXES
            ]
            if not paths:
                raise VideoWorkflowError(
                    "video-material revision contains no supported files: "
                    f"{request.material_revision}"
                )
            return paths
        source = self._material_source
        if source is None:
            api_key = self._settings.pexels.api_key.get_secret_value()
            if not api_key:
                raise VideoWorkflowError(
                    "Pexels API key is missing; edit .local/secrets.toml or pass "
                    "--material-revision"
                )
            source = PexelsMaterialSource(api_key=api_key)
        downloads = source.acquire(
            terms=spec.material_terms,
            destination=staging / "materials",
            max_files=request.material_count or self._automatic_material_count(spec),
        )
        return [item.path for item in downloads]

    @staticmethod
    def _automatic_material_count(spec: VideoScriptSpec) -> int:
        return min(
            24,
            max(
                6,
                len(spec.material_terms) * 4,
                math.ceil(len(spec.narration) / 120),
            ),
        )

    def _prepare_bgm(self, directory: Path | None, staging: Path) -> Path | None:
        if directory is None:
            return None
        if not directory.is_dir():
            raise VideoWorkflowError(f"BGM directory is missing: {directory}")
        candidates = sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in self._AUDIO_SUFFIXES
        )
        if not candidates:
            raise VideoWorkflowError(
                f"BGM directory contains no supported audio: {directory}"
            )
        selected = random.SystemRandom().choice(candidates)
        destination = staging / "bgm" / f"selected{selected.suffix.lower()}"
        destination.parent.mkdir(parents=True)
        shutil.copy2(selected, destination)
        return destination

    def _prepare_narration(
        self,
        request: VideoRenderRequest,
        spec: VideoScriptSpec,
        staging: Path,
    ) -> NarrationResult:
        if request.narration_audio is not None:
            assert request.subtitles is not None
            narration_root = staging / "narration"
            narration_root.mkdir(parents=True)
            local_audio = narration_root / f"narration{request.narration_audio.suffix.lower()}"
            local_subtitles = narration_root / "subtitles.srt"
            shutil.copy2(request.narration_audio, local_audio)
            shutil.copy2(request.subtitles, local_subtitles)
            (narration_root / "script.txt").write_text(
                spec.narration + "\n",
                encoding="utf-8",
            )
            return NarrationResult(
                root=narration_root,
                audio_path=local_audio,
                subtitles_path=local_subtitles,
                voice="local",
            )
        narrator = self._narrator or EdgeTtsNarrator(
            timeout_seconds=self._settings.tts.request_timeout_seconds
        )
        return narrator.synthesize(
            narration=spec.narration,
            voice=self._settings.tts.voice,
            output_root=staging / "narration",
        )
