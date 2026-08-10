from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ApprovalScope(StrEnum):
    ARTICLE = "article"
    COVER = "cover"
    ARTICLE_PUBLICATION = "article-publication"
    VIDEO_SCRIPT = "video-script"
    VIDEO = "video"
    SOCIAL_PUBLICATION = "social-publication"


class StageState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: ApprovalScope
    revision: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+@,=-]*$",
    )
    content_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approved_at: datetime


class PublicationClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    acquired: bool
    prior_state: str | None = None


class ApprovalLedger:
    """Persist approval for an exact artifact revision inside a Product."""

    def __init__(self, product_root: Path | str) -> None:
        self.product_root = Path(product_root)
        self.state_path = self.product_root / "state.sqlite3"

    def record(
        self,
        scope: ApprovalScope,
        revision: str,
        content_digest: str | None = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            scope=scope,
            revision=revision,
            content_digest=content_digest,
            approved_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute(
                """
                INSERT INTO approvals(scope, revision, content_digest, approved_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, revision) DO UPDATE SET
                    content_digest = excluded.content_digest,
                    approved_at = excluded.approved_at
                """,
                (
                    record.scope.value,
                    record.revision,
                    record.content_digest,
                    record.approved_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT scope, revision, content_digest, approved_at
                FROM approvals WHERE scope = ? AND revision = ?
                """,
                (record.scope.value, record.revision),
            ).fetchone()
        return self._from_row(row)

    def list(self) -> list[ApprovalRecord]:
        if not self.state_path.is_file():
            return []
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                "SELECT scope, revision, content_digest, approved_at FROM approvals ORDER BY id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def has(
        self,
        scope: ApprovalScope,
        revision: str,
        content_digest: str | None = None,
    ) -> bool:
        if not self.state_path.is_file():
            return False
        with self._connect() as connection:
            self._initialize(connection)
            if content_digest is None:
                row = connection.execute(
                    "SELECT 1 FROM approvals WHERE scope = ? AND revision = ?",
                    (scope.value, revision),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT 1 FROM approvals
                    WHERE scope = ? AND revision = ? AND content_digest = ?
                    """,
                    (scope.value, revision, content_digest),
                ).fetchone()
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.state_path)

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals(
                id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL,
                revision TEXT NOT NULL,
                content_digest TEXT,
                approved_at TEXT NOT NULL,
                UNIQUE(scope, revision)
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
        }
        if "content_digest" not in columns:
            connection.execute("ALTER TABLE approvals ADD COLUMN content_digest TEXT")

    @staticmethod
    def _from_row(row: tuple[str, str, str | None, str]) -> ApprovalRecord:
        return ApprovalRecord(
            scope=ApprovalScope(row[0]),
            revision=row[1],
            content_digest=row[2],
            approved_at=datetime.fromisoformat(row[3]),
        )


class PublicationLedger:
    """Persist one independently retryable destination result per idempotency key."""

    def __init__(self, product_root: Path | str) -> None:
        self.state_path = Path(product_root) / "state.sqlite3"

    def get_state(self, destination: str, idempotency_key: str) -> str | None:
        if not self.state_path.is_file():
            return None
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT state FROM publication_results
                WHERE destination = ? AND idempotency_key = ?
                """,
                (destination, idempotency_key),
            ).fetchone()
        return row[0] if row else None

    def claim(
        self,
        destination: str,
        idempotency_key: str,
        *,
        retryable_states: frozenset[str] = frozenset(("failed", "waiting_for_user")),
    ) -> PublicationClaim:
        """Atomically reserve one external publication before crossing its seam."""

        with sqlite3.connect(self.state_path, timeout=30, isolation_level=None) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT state FROM publication_results
                WHERE destination = ? AND idempotency_key = ?
                """,
                (destination, idempotency_key),
            ).fetchone()
            prior_state = row[0] if row else None
            if row is None:
                connection.execute(
                    """
                    INSERT INTO publication_results(
                        destination, idempotency_key, state, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        destination,
                        idempotency_key,
                        "running",
                        datetime.now(UTC).isoformat(),
                    ),
                )
                acquired = True
            elif prior_state in retryable_states:
                connection.execute(
                    """
                    UPDATE publication_results
                    SET state = ?, updated_at = ?
                    WHERE destination = ? AND idempotency_key = ? AND state = ?
                    """,
                    (
                        "running",
                        datetime.now(UTC).isoformat(),
                        destination,
                        idempotency_key,
                        prior_state,
                    ),
                )
                acquired = connection.total_changes == 1
            else:
                acquired = False
            connection.execute("COMMIT")
        return PublicationClaim(acquired=acquired, prior_state=prior_state)

    def record_state(self, destination: str, idempotency_key: str, state: str) -> None:
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            connection.execute(
                """
                INSERT INTO publication_results(destination, idempotency_key, state, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(destination, idempotency_key)
                DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at
                """,
                (destination, idempotency_key, state, datetime.now(UTC).isoformat()),
            )

    def list(self) -> list[dict[str, str]]:
        if not self.state_path.is_file():
            return []
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            rows = connection.execute(
                """
                SELECT destination, idempotency_key, state, updated_at
                FROM publication_results ORDER BY id
                """
            ).fetchall()
        return [
            {
                "destination": row[0],
                "idempotencyKey": row[1],
                "state": row[2],
                "updatedAt": row[3],
            }
            for row in rows
        ]

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS publication_results(
                id INTEGER PRIMARY KEY,
                destination TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(destination, idempotency_key)
            )
            """
        )


class StageAttempt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    run_id: str
    stage: str
    idempotency_key: str
    args: tuple[str, ...]
    state: StageState
    exit_code: int | None = None
    output: dict = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None


class PipelineRunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    mode: str
    state: StageState
    commands: tuple[dict, ...]
    results: tuple[dict, ...] = ()
    started_at: datetime
    finished_at: datetime | None = None


class StageAttemptLedger:
    """Durable, redacted attempt history for local and external pipeline stages."""

    def __init__(self, product_root: Path | str) -> None:
        self.state_path = Path(product_root) / "state.sqlite3"

    def start(
        self,
        stage: str,
        idempotency_key: str,
        args: tuple[str, ...],
        *,
        run_id: str | None = None,
    ) -> StageAttempt:
        started_at = datetime.now(UTC)
        run_id = run_id or f"run-{uuid4()}"
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            cursor = connection.execute(
                """
                INSERT INTO stage_attempts(
                    run_id, stage, idempotency_key, args_json, state, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    stage,
                    idempotency_key,
                    json.dumps(args, ensure_ascii=False),
                    StageState.RUNNING.value,
                    started_at.isoformat(),
                ),
            )
            attempt_id = int(cursor.lastrowid)
        return StageAttempt(
            id=attempt_id,
            run_id=run_id,
            stage=stage,
            idempotency_key=idempotency_key,
            args=args,
            state=StageState.RUNNING,
            started_at=started_at,
        )

    def finish(
        self,
        attempt_id: int,
        state: StageState,
        exit_code: int | None,
        output: dict,
    ) -> StageAttempt:
        if state in {StageState.PLANNED, StageState.RUNNING, StageState.SKIPPED, StageState.BLOCKED}:
            raise ValueError(f"attempt cannot finish in state: {state.value}")
        finished_at = datetime.now(UTC)
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            connection.execute(
                """
                UPDATE stage_attempts
                SET state = ?, exit_code = ?, output_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    state.value,
                    exit_code,
                    json.dumps(output, ensure_ascii=False),
                    finished_at.isoformat(),
                    attempt_id,
                ),
            )
            row = connection.execute(
                """
                SELECT id, run_id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"stage attempt not found: {attempt_id}")
        return self._from_row(row)

    def record_terminal(
        self,
        *,
        run_id: str,
        stage: str,
        idempotency_key: str,
        args: tuple[str, ...],
        state: StageState,
        output: dict,
    ) -> StageAttempt:
        if state not in {
            StageState.SUCCEEDED,
            StageState.FAILED,
            StageState.PARTIAL,
            StageState.UNKNOWN,
            StageState.WAITING_FOR_USER,
        }:
            raise ValueError(f"terminal record cannot use state: {state.value}")
        timestamp = datetime.now(UTC)
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            cursor = connection.execute(
                """
                INSERT INTO stage_attempts(
                    run_id, stage, idempotency_key, args_json, state,
                    output_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    stage,
                    idempotency_key,
                    json.dumps(args, ensure_ascii=False),
                    state.value,
                    json.dumps(output, ensure_ascii=False),
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            attempt_id = int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT id, run_id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
        return self._from_row(row)

    def latest(self, stage: str) -> StageAttempt | None:
        if not self.state_path.is_file():
            return None
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT id, run_id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts WHERE stage = ? ORDER BY id DESC LIMIT 1
                """,
                (stage,),
            ).fetchone()
        return self._from_row(row) if row else None

    def latest_exact(self, stage: str, idempotency_key: str) -> StageAttempt | None:
        if not self.state_path.is_file():
            return None
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT id, run_id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts
                WHERE stage = ? AND idempotency_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (stage, idempotency_key),
            ).fetchone()
        return self._from_row(row) if row else None

    def list(self) -> list[StageAttempt]:
        if not self.state_path.is_file():
            return []
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            rows = connection.execute(
                """
                SELECT id, run_id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts ORDER BY id
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def start_run(self, run_id: str, mode: str, commands: tuple[dict, ...]) -> None:
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            connection.execute(
                """
                INSERT INTO pipeline_runs(
                    run_id, mode, state, commands_json, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    StageState.RUNNING.value,
                    json.dumps(commands, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def finish_run(
        self,
        run_id: str,
        state: StageState,
        results: tuple[dict, ...],
    ) -> PipelineRunRecord:
        finished_at = datetime.now(UTC)
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            connection.execute(
                """
                UPDATE pipeline_runs
                SET state = ?, results_json = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    state.value,
                    json.dumps(results, ensure_ascii=False),
                    finished_at.isoformat(),
                    run_id,
                ),
            )
            row = connection.execute(
                """
                SELECT run_id, mode, state, commands_json, results_json,
                       started_at, finished_at
                FROM pipeline_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"pipeline run not found: {run_id}")
        return self._run_from_row(row)

    def list_runs(self) -> list[PipelineRunRecord]:
        if not self.state_path.is_file():
            return []
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            rows = connection.execute(
                """
                SELECT run_id, mode, state, commands_json, results_json,
                       started_at, finished_at
                FROM pipeline_runs ORDER BY id
                """
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_attempts(
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                stage TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                args_json TEXT NOT NULL,
                state TEXT NOT NULL,
                exit_code INTEGER,
                output_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(stage_attempts)").fetchall()
        }
        if "run_id" not in columns:
            connection.execute("ALTER TABLE stage_attempts ADD COLUMN run_id TEXT")
        connection.execute(
            "UPDATE stage_attempts SET run_id = 'legacy-' || id WHERE run_id IS NULL"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_runs(
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL,
                state TEXT NOT NULL,
                commands_json TEXT NOT NULL,
                results_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )

    @staticmethod
    def _from_row(row) -> StageAttempt:
        return StageAttempt(
            id=row[0],
            run_id=row[1],
            stage=row[2],
            idempotency_key=row[3],
            args=tuple(json.loads(row[4])),
            state=StageState(row[5]),
            exit_code=row[6],
            output=json.loads(row[7] or "{}"),
            started_at=datetime.fromisoformat(row[8]),
            finished_at=datetime.fromisoformat(row[9]) if row[9] else None,
        )

    @staticmethod
    def _run_from_row(row) -> PipelineRunRecord:
        return PipelineRunRecord(
            run_id=row[0],
            mode=row[1],
            state=StageState(row[2]),
            commands=tuple(json.loads(row[3])),
            results=tuple(json.loads(row[4] or "[]")),
            started_at=datetime.fromisoformat(row[5]),
            finished_at=datetime.fromisoformat(row[6]) if row[6] else None,
        )
