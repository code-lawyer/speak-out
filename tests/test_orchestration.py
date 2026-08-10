import json
import subprocess

import pytest

from agent_content_pipeline.orchestration import (
    PipelineOrchestrator,
    RetryNotAllowed,
    StageCommand,
    StageState,
)
from agent_content_pipeline.state import StageAttemptLedger


class FakeExecutor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, args):
        self.calls.append(tuple(args))
        return self.results.pop(0)


class RaisingExecutor:
    def run(self, args):
        raise OSError("process could not start")


def completed(returncode: int, payload: dict):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_run_keeps_independent_branch_success_when_another_branch_fails(tmp_path):
    executor = FakeExecutor(
        [
            completed(1, {"ok": False, "state": "failed", "error": "article failed"}),
            completed(0, {"ok": True, "artifact": {"revision": "v001"}}),
        ]
    )
    orchestrator = PipelineOrchestrator(StageAttemptLedger(tmp_path), executor)
    commands = [
        StageCommand(stage="article", idempotency_key="article-key", args=("article", "publish")),
        StageCommand(stage="video", idempotency_key="video-key", args=("video", "render")),
    ]

    result = orchestrator.run(commands, execute=True)

    assert [item.state for item in result.stages] == [StageState.FAILED, StageState.SUCCEEDED]
    assert executor.calls == [("article", "publish"), ("video", "render")]
    attempts = StageAttemptLedger(tmp_path).list()
    assert [(item.stage, item.state) for item in attempts] == [
        ("article", StageState.FAILED),
        ("video", StageState.SUCCEEDED),
    ]


def test_run_is_dry_by_default_and_returns_exact_commands(tmp_path):
    executor = FakeExecutor([])
    orchestrator = PipelineOrchestrator(StageAttemptLedger(tmp_path), executor)
    command = StageCommand(
        stage="social:bilibili",
        idempotency_key="social-key",
        args=("social", "publish", "--platform", "bilibili"),
    )

    result = orchestrator.run([command], execute=False)

    assert result.mode == "dry-run"
    assert result.stages[0].state == StageState.PLANNED
    assert result.stages[0].args == command.args
    assert executor.calls == []
    assert StageAttemptLedger(tmp_path).list() == []


def test_retry_runs_only_the_latest_failed_stage_and_refuses_unknown(tmp_path):
    ledger = StageAttemptLedger(tmp_path)
    failed = ledger.start("video", "video-key", ("video", "render"))
    ledger.finish(failed.id, StageState.FAILED, 1, {"error": "ffmpeg"})
    unknown = ledger.start("article", "article-key", ("article", "publish"))
    ledger.finish(unknown.id, StageState.UNKNOWN, 1, {"error": "timeout"})
    executor = FakeExecutor([completed(0, {"ok": True})])
    orchestrator = PipelineOrchestrator(ledger, executor)

    retried = orchestrator.retry("video", execute=True)

    assert retried.stages[0].state == StageState.SUCCEEDED
    assert executor.calls == [("video", "render")]
    with pytest.raises(RetryNotAllowed):
        orchestrator.retry("article", execute=True)
    assert executor.calls == [("video", "render")]


def test_executor_start_failure_is_recorded_instead_of_leaving_running_attempt(tmp_path):
    ledger = StageAttemptLedger(tmp_path)
    orchestrator = PipelineOrchestrator(ledger, RaisingExecutor())
    command = StageCommand(
        stage="video",
        idempotency_key="video-key",
        args=("video", "render"),
    )

    result = orchestrator.run([command], execute=True)

    assert result.stages[0].state == StageState.FAILED
    assert result.stages[0].output["errorType"] == "OSError"
    attempt = ledger.latest("video")
    assert attempt is not None
    assert attempt.state == StageState.FAILED
    assert attempt.finished_at is not None
