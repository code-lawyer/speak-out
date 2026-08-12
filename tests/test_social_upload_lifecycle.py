import hashlib
import json
from datetime import date

import pytest

from agent_content_pipeline.pipeline import (
    social_publication_approval_key,
    social_publication_content_digest,
)
from agent_content_pipeline.social.models import (
    SocialPlatform,
    SocialPublicationState,
    SocialPublishResult,
)
from agent_content_pipeline.social.workflow import (
    SocialPublicationRequest,
    SocialPublicationWorkflow,
    SocialPublicationWorkflowError,
)
from agent_content_pipeline.state import ApprovalLedger, ApprovalScope
from agent_content_pipeline.workspace import (
    ArtifactKind,
    ArtifactRevisionRequest,
    ProductCreateRequest,
    ProductWorkspace,
)


def test_interrupted_upload_snapshot_is_retained_reused_and_only_removed_after_completion(
    tmp_path,
):
    workspace = ProductWorkspace(tmp_path / "workspace")
    product = workspace.create(
        ProductCreateRequest(
            title="异步上传生命周期",
            slug="async-upload-lifecycle",
            created_on=date(2026, 8, 12),
        )
    )
    render_work = product.root / "video" / "work" / "done"
    (render_work / "output").mkdir(parents=True)
    (render_work / "output" / "final.mp4").write_bytes(b"approved-video")
    video = workspace.commit_revision_directory(
        product,
        ArtifactKind.VIDEO_RENDER,
        render_work,
    )
    copy = workspace.add_revision(
        product,
        ArtifactRevisionRequest(
            kind=ArtifactKind.SOCIAL_COPY,
            files={
                "copy.json": json.dumps(
                    {
                        "schemaVersion": 1,
                        "platforms": {
                            "xiaohongshu": {"title": "小红书", "body": "正文", "tags": ["AI"]},
                            "douyin": {"title": "抖音", "body": "正文", "tags": ["AI"]},
                            "bilibili": {
                                "title": "B站",
                                "body": "简介",
                                "tags": ["AI"],
                                "category": "知识",
                            },
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            },
        ),
    )
    platform = SocialPlatform.XIAOHONGSHU
    key = social_publication_approval_key("v001", "v001", platform)
    approvals = ApprovalLedger(product.root)
    approvals.record(ApprovalScope.VIDEO, "v001", video.digest)
    approvals.record(
        ApprovalScope.SOCIAL_PUBLICATION,
        key,
        social_publication_content_digest(video.digest, copy.digest, platform),
    )

    class Session:
        websocket_url = "ws://fake/xiaohongshu"
        process_id = None

    class Driver:
        def launch(self, *, platform, start_url):
            return Session()

        def stop_launched_session(self, session):
            raise AssertionError("the attached session must not be stopped")

    class Cdp:
        def close(self):
            pass

    class Page:
        def screenshot(self, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"diagnostic")
            return path

    published_paths = []

    class Publisher:
        def __init__(self, _platform):
            pass

        def publish(self, page, spec, *, lifecycle):
            published_paths.append(spec.video_path)
            assert spec.video_path.read_bytes() == b"approved-video"
            if len(published_paths) == 1:
                lifecycle.mark_upload_started()
                raise RuntimeError("CDP disconnected while the upload was in progress")
            assert spec.video_path == published_paths[0]
            lifecycle.mark_upload_completed()
            return SocialPublishResult(
                platform=spec.platform,
                state=SocialPublicationState.FAILED,
                message="upload completed; metadata contract deliberately stopped this test",
                upload_state=lifecycle.upload_state,
            )

    workflow = SocialPublicationWorkflow(
        workspace=workspace,
        product=product,
        driver=Driver(),
        cdp_factory=lambda _url: Cdp(),
        page_attach=lambda _cdp: Page(),
        publisher_factory=Publisher,
        approvals=approvals,
    )
    request = SocialPublicationRequest(
        platform=platform,
        video_revision="v001",
        copy_revision="v001",
        execute=True,
    )

    first = workflow.publish(request)

    staging = product.root / "publish" / ".staging"
    assert first.state == SocialPublicationState.FAILED.value
    assert "safe to retry" in first.message
    assert first.snapshot_retained is True
    assert len(list(staging.glob("*.mp4"))) == 1
    assert len(list(staging.glob("*.snapshot.json"))) == 1

    snapshot = next(staging.glob("*.mp4"))
    sidecar_path = next(staging.glob("*.snapshot.json"))
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    snapshot.write_bytes(b"unapproved-video")
    sidecar["fileSha256"] = hashlib.sha256(b"unapproved-video").hexdigest()
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    with pytest.raises(
        SocialPublicationWorkflowError,
        match="approved video artifact",
    ):
        workflow.publish(request)

    snapshot.write_bytes(b"approved-video")
    sidecar["fileSha256"] = hashlib.sha256(b"approved-video").hexdigest()
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    second = workflow.publish(request)

    assert second.state == SocialPublicationState.FAILED.value
    assert second.snapshot_retained is False
    assert len(published_paths) == 2
    assert not list(staging.glob("*.mp4"))
    assert not list(staging.glob("*.snapshot.json"))
