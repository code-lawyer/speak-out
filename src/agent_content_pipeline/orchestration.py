from __future__ import annotations

import json
import subprocess
import sys
from enum import StrEnum
from typing import Any, Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .state import PublicationLedger, StageAttemptLedger, StageState
from .security import redact_sensitive_data


class StageCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?::[a-z0-9-]+)?$")
    idempotency_key: str = Field(min_length=1, max_length=512)
    args: tuple[str, ...] = Field(min_length=1)
    blockers: tuple[str, ...] = ()


class StageRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    idempotency_key: str
    args: tuple[str, ...]
    state: StageState
    output: dict[str, Any] = Field(default_factory=dict)
    attempt_id: int | None = None


class PipelineRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    mode: str
    stages: tuple[StageRunResult, ...]


class StageExecutor(Protocol):
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class SubprocessStageExecutor:
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (sys.executable, "-m", "agent_content_pipeline.cli", *args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


class RetryNotAllowed(RuntimeError):
    pass


class ReconciliationOutcome(StrEnum):
    ABSENT = "absent"
    SUCCEEDED = "succeeded"


class ReconciliationNotAllowed(RuntimeError):
    pass


_redact = redact_sensitive_data


class PipelineOrchestrator:
    """Execute independent CLI stages while preserving exact retry evidence."""

    def __init__(self, ledger: StageAttemptLedger, executor: StageExecutor | None = None) -> None:
        self._ledger = ledger
        self._executor = executor or SubprocessStageExecutor()

    def run(
        self,
        commands: Sequence[StageCommand],
        *,
        execute: bool = False,
    ) -> PipelineRunResult:
        run_id = f"run-{uuid4()}"
        if not execute:
            preview_results: list[StageRunResult] = []
            for command in commands:
                if command.blockers:
                    preview_results.append(
                        StageRunResult(
                            stage=command.stage,
                            idempotency_key=command.idempotency_key,
                            args=command.args,
                            state=StageState.BLOCKED,
                            output={"blockers": list(command.blockers)},
                        )
                    )
                    continue
                prior = self._ledger.latest_exact(
                    command.stage,
                    command.idempotency_key,
                )
                if prior is None:
                    state = StageState.PLANNED
                    output: dict[str, Any] = {}
                elif prior.state == StageState.SUCCEEDED:
                    state = StageState.SKIPPED
                    output = {
                        "reason": "exact stage already succeeded",
                        "priorAttempt": prior.id,
                    }
                else:
                    state = StageState.BLOCKED
                    output = {
                        "blockers": [
                            f"prior state is {prior.state.value}; "
                            "use acp retry or reconcile when allowed"
                        ],
                        "priorAttempt": prior.id,
                    }
                preview_results.append(
                    StageRunResult(
                        stage=command.stage,
                        idempotency_key=command.idempotency_key,
                        args=command.args,
                        state=state,
                        output=output,
                        attempt_id=prior.id if prior is not None else None,
                    )
                )
            return PipelineRunResult(
                run_id=run_id,
                mode="dry-run",
                stages=tuple(preview_results),
            )

        self._ledger.start_run(
            run_id,
            "execute",
            tuple(command.model_dump(mode="json") for command in commands),
        )
        results: list[StageRunResult] = []
        for command in commands:
            if command.blockers:
                results.append(
                    StageRunResult(
                        stage=command.stage,
                        idempotency_key=command.idempotency_key,
                        args=command.args,
                        state=StageState.BLOCKED,
                        output={"blockers": list(command.blockers)},
                    )
                )
                continue
            prior = self._ledger.latest_exact(command.stage, command.idempotency_key)
            if prior is not None:
                if prior.state == StageState.SUCCEEDED:
                    results.append(
                        StageRunResult(
                            stage=command.stage,
                            idempotency_key=command.idempotency_key,
                            args=command.args,
                            state=StageState.SKIPPED,
                            output={"reason": "exact stage already succeeded"},
                            attempt_id=prior.id,
                        )
                    )
                    continue
                results.append(
                    StageRunResult(
                        stage=command.stage,
                        idempotency_key=command.idempotency_key,
                        args=command.args,
                        state=StageState.BLOCKED,
                        output={
                            "reason": f"prior state is {prior.state.value}; use acp retry when allowed"
                        },
                        attempt_id=prior.id,
                    )
                )
                continue
            results.append(self._execute(command, run_id))
        run_state = self._overall_state(results)
        self._ledger.finish_run(
            run_id,
            run_state,
            tuple(item.model_dump(mode="json") for item in results),
        )
        return PipelineRunResult(run_id=run_id, mode="execute", stages=tuple(results))

    def retry(self, stage: str, *, execute: bool = False) -> PipelineRunResult:
        run_id = f"run-{uuid4()}"
        prior = self._ledger.latest(stage)
        if prior is None:
            raise RetryNotAllowed(f"stage has no prior attempt: {stage}")
        if prior.state not in {StageState.FAILED, StageState.WAITING_FOR_USER}:
            raise RetryNotAllowed(
                f"stage state {prior.state.value} is not safe to retry: {stage}"
            )
        command = StageCommand(
            stage=prior.stage,
            idempotency_key=prior.idempotency_key,
            args=prior.args,
        )
        if not execute:
            return PipelineRunResult(
                run_id=run_id,
                mode="dry-run",
                stages=(
                    StageRunResult(
                        stage=command.stage,
                        idempotency_key=command.idempotency_key,
                        args=command.args,
                        state=StageState.PLANNED,
                        output={"retryOfAttempt": prior.id},
                    ),
                ),
            )
        self._ledger.start_run(
            run_id,
            "retry",
            (command.model_dump(mode="json"),),
        )
        stage_result = self._execute(command, run_id)
        self._ledger.finish_run(
            run_id,
            self._overall_state([stage_result]),
            (stage_result.model_dump(mode="json"),),
        )
        return PipelineRunResult(run_id=run_id, mode="retry", stages=(stage_result,))

    def reconcile(
        self,
        stage: str,
        outcome: ReconciliationOutcome,
        *,
        evidence: str,
        execute: bool = False,
    ) -> PipelineRunResult:
        run_id = f"run-{uuid4()}"
        prior = self._ledger.latest(stage)
        if prior is None:
            raise ReconciliationNotAllowed(f"stage has no prior attempt: {stage}")
        allowed_states = {
            ReconciliationOutcome.ABSENT: {StageState.UNKNOWN, StageState.RUNNING},
            ReconciliationOutcome.SUCCEEDED: {
                StageState.UNKNOWN,
                StageState.RUNNING,
                StageState.PARTIAL,
            },
        }
        if prior.state not in allowed_states[outcome]:
            raise ReconciliationNotAllowed(
                f"cannot reconcile {prior.state.value} as {outcome.value}: {stage}"
            )
        evidence = evidence.strip()
        if not evidence:
            raise ReconciliationNotAllowed("reconciliation evidence is required")
        resolved_state = (
            StageState.FAILED
            if outcome == ReconciliationOutcome.ABSENT
            else StageState.SUCCEEDED
        )
        output = {
            "reconciliation": {
                "priorAttempt": prior.id,
                "priorState": prior.state.value,
                "outcome": outcome.value,
                "evidence": evidence,
            }
        }
        if not execute:
            return PipelineRunResult(
                run_id=run_id,
                mode="dry-run",
                stages=(
                    StageRunResult(
                        stage=prior.stage,
                        idempotency_key=prior.idempotency_key,
                        args=prior.args,
                        state=StageState.PLANNED,
                        output=output,
                    ),
                ),
            )

        command_record = {
            "stage": prior.stage,
            "idempotency_key": prior.idempotency_key,
            "args": list(prior.args),
            "reconciliation": output["reconciliation"],
        }
        self._ledger.start_run(run_id, "reconcile", (command_record,))
        attempt = self._ledger.record_terminal(
            run_id=run_id,
            stage=prior.stage,
            idempotency_key=prior.idempotency_key,
            args=prior.args,
            state=resolved_state,
            output=output,
        )
        self._record_publication_reconciliation(
            prior.stage,
            prior.idempotency_key,
            outcome,
        )
        result = StageRunResult(
            stage=prior.stage,
            idempotency_key=prior.idempotency_key,
            args=prior.args,
            state=resolved_state,
            output=output,
            attempt_id=attempt.id,
        )
        self._ledger.finish_run(
            run_id,
            resolved_state,
            (result.model_dump(mode="json"),),
        )
        return PipelineRunResult(run_id=run_id, mode="reconcile", stages=(result,))

    def _record_publication_reconciliation(
        self,
        stage: str,
        idempotency_key: str,
        outcome: ReconciliationOutcome,
    ) -> None:
        if stage == "article":
            destination = "website-wechat"
            succeeded_state = "succeeded"
        elif stage.startswith("social:"):
            destination = stage
            succeeded_state = "submitted"
        else:
            return
        state = "failed" if outcome == ReconciliationOutcome.ABSENT else succeeded_state
        PublicationLedger(self._ledger.state_path.parent).record_state(
            destination,
            idempotency_key,
            state,
        )

    def _execute(self, command: StageCommand, run_id: str) -> StageRunResult:
        attempt = self._ledger.start(
            command.stage,
            command.idempotency_key,
            command.args,
            run_id=run_id,
        )
        try:
            completed = self._executor.run(command.args)
        except Exception as error:
            state = StageState.FAILED
            output = {
                "error": "stage executor could not start",
                "errorType": type(error).__name__,
            }
            finished = self._ledger.finish(attempt.id, state, None, output)
        else:
            output = self._parse_output(completed.stdout, completed.stderr)
            state = self._classify(completed.returncode, output)
            finished = self._ledger.finish(
                attempt.id,
                state,
                completed.returncode,
                output,
            )
        return StageRunResult(
            stage=command.stage,
            idempotency_key=command.idempotency_key,
            args=command.args,
            state=state,
            output=finished.output,
            attempt_id=finished.id,
        )

    @staticmethod
    def _overall_state(results: Sequence[StageRunResult]) -> StageState:
        states = {item.state for item in results}
        if states <= {StageState.SUCCEEDED, StageState.SKIPPED}:
            return StageState.SUCCEEDED
        if StageState.UNKNOWN in states:
            return StageState.UNKNOWN
        if StageState.PARTIAL in states:
            return StageState.PARTIAL
        if StageState.WAITING_FOR_USER in states:
            return StageState.WAITING_FOR_USER
        if StageState.BLOCKED in states and not states & {
            StageState.SUCCEEDED,
            StageState.SKIPPED,
        }:
            return StageState.BLOCKED
        if states & {StageState.SUCCEEDED, StageState.SKIPPED}:
            return StageState.PARTIAL
        return StageState.FAILED

    @staticmethod
    def _parse_output(stdout: str, stderr: str) -> dict[str, Any]:
        lines = [line for line in stdout.splitlines() if line.strip()]
        payload: dict[str, Any]
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except (ValueError, TypeError):
            payload = {"stdout": stdout[-4000:]}
        if stderr.strip():
            payload["stderr"] = stderr[-4000:]
        return _redact(payload)

    @staticmethod
    def _classify(returncode: int, output: dict[str, Any]) -> StageState:
        raw_state = output.get("state")
        mapping = {
            "succeeded": StageState.SUCCEEDED,
            "submitted": StageState.SUCCEEDED,
            "failed": StageState.FAILED,
            "waiting_for_user": StageState.WAITING_FOR_USER,
            "partial": StageState.PARTIAL,
            "unknown": StageState.UNKNOWN,
        }
        if raw_state in mapping:
            return mapping[raw_state]
        return StageState.SUCCEEDED if returncode == 0 else StageState.FAILED


__all__ = [
    "PipelineOrchestrator",
    "PipelineRunResult",
    "ReconciliationNotAllowed",
    "ReconciliationOutcome",
    "RetryNotAllowed",
    "StageCommand",
    "StageRunResult",
    "StageState",
]
