import json
import subprocess

import pytest

from agent_content_pipeline.orchestration import (
    PipelineOrchestrator,
    ReconciliationNotAllowed,
    ReconciliationOutcome,
    RetryNotAllowed,
    StageCommand,
    StageState,
)
from agent_content_pipeline.state import StageAttemptLedger
from agent_content_pipeline.state import PublicationLedger


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
    assert result.run_id
    assert {item.run_id for item in attempts} == {result.run_id}
    runs = StageAttemptLedger(tmp_path).list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == result.run_id
    assert runs[0].state == StageState.PARTIAL


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
    assert StageAttemptLedger(tmp_path).list_runs() == []


def test_preflight_blockers_are_visible_and_never_execute(tmp_path):
    executor = FakeExecutor([])
    orchestrator = PipelineOrchestrator(StageAttemptLedger(tmp_path), executor)
    command = StageCommand(
        stage="video",
        idempotency_key="video-key",
        args=("video", "render"),
        blockers=(
            "missing exact approval: video-script:v001",
            "Edge TTS data-transfer approval is required",
        ),
    )

    preview = orchestrator.run([command], execute=False)

    assert preview.stages[0].state == StageState.BLOCKED
    assert preview.stages[0].output["blockers"] == list(command.blockers)
    executed = orchestrator.run([command], execute=True)
    assert executed.stages[0].state == StageState.BLOCKED
    assert executor.calls == []
    assert StageAttemptLedger(tmp_path).list() == []
    assert StageAttemptLedger(tmp_path).list_runs()[-1].state == StageState.BLOCKED


def test_success_plus_preflight_blocker_is_reported_as_partial(tmp_path):
    executor = FakeExecutor([completed(0, {"ok": True})])
    ledger = StageAttemptLedger(tmp_path)
    orchestrator = PipelineOrchestrator(ledger, executor)

    result = orchestrator.run(
        [
            StageCommand(
                stage="article",
                idempotency_key="article-key",
                args=("article", "publish"),
            ),
            StageCommand(
                stage="video",
                idempotency_key="video-key",
                args=("video", "render"),
                blockers=("missing video approval",),
            ),
        ],
        execute=True,
    )

    assert [item.state for item in result.stages] == [
        StageState.SUCCEEDED,
        StageState.BLOCKED,
    ]
    assert ledger.list_runs()[-1].state == StageState.PARTIAL


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
    assert retried.run_id
    assert ledger.latest("video").run_id == retried.run_id
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


def test_reconcile_unknown_as_absent_appends_evidence_and_enables_exact_retry(tmp_path):
    ledger = StageAttemptLedger(tmp_path)
    unknown = ledger.start(
        "social:bilibili",
        "social-key",
        ("social", "publish", "--platform", "bilibili"),
    )
    ledger.finish(unknown.id, StageState.UNKNOWN, 1, {"error": "timeout"})
    PublicationLedger(tmp_path).record_state("social:bilibili", "social-key", "unknown")
    executor = FakeExecutor([completed(0, {"ok": True, "state": "submitted"})])
    orchestrator = PipelineOrchestrator(ledger, executor)

    preview = orchestrator.reconcile(
        "social:bilibili",
        ReconciliationOutcome.ABSENT,
        evidence="Checked creator center; no matching submission exists.",
        execute=False,
    )

    assert preview.mode == "dry-run"
    assert len(ledger.list()) == 1
    reconciled = orchestrator.reconcile(
        "social:bilibili",
        ReconciliationOutcome.ABSENT,
        evidence="Checked creator center; no matching submission exists.",
        execute=True,
    )
    assert reconciled.stages[0].state == StageState.FAILED
    assert reconciled.stages[0].output["reconciliation"]["outcome"] == "absent"
    assert PublicationLedger(tmp_path).get_state("social:bilibili", "social-key") == "failed"

    retried = orchestrator.retry("social:bilibili", execute=True)
    assert retried.stages[0].state == StageState.SUCCEEDED


def test_reconcile_partial_as_absent_is_refused(tmp_path):
    ledger = StageAttemptLedger(tmp_path)
    partial = ledger.start("article", "article-key", ("article", "publish"))
    ledger.finish(partial.id, StageState.PARTIAL, 1, {"state": "partial"})
    orchestrator = PipelineOrchestrator(ledger, FakeExecutor([]))

    with pytest.raises(ReconciliationNotAllowed):
        orchestrator.reconcile(
            "article",
            ReconciliationOutcome.ABSENT,
            evidence="Website exists but WeChat failed.",
            execute=True,
        )
