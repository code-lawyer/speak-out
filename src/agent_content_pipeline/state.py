from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

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
    approved_at: datetime


class ApprovalLedger:
    """Persist approval for an exact artifact revision inside a Product."""

    def __init__(self, product_root: Path | str) -> None:
        self.product_root = Path(product_root)
        self.state_path = self.product_root / "state.sqlite3"

    def record(self, scope: ApprovalScope, revision: str) -> ApprovalRecord:
        record = ApprovalRecord(
            scope=scope,
            revision=revision,
            approved_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute(
                """
                INSERT INTO approvals(scope, revision, approved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope, revision) DO NOTHING
                """,
                (record.scope.value, record.revision, record.approved_at.isoformat()),
            )
            row = connection.execute(
                "SELECT scope, revision, approved_at FROM approvals WHERE scope = ? AND revision = ?",
                (record.scope.value, record.revision),
            ).fetchone()
        return self._from_row(row)

    def list(self) -> list[ApprovalRecord]:
        if not self.state_path.is_file():
            return []
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                "SELECT scope, revision, approved_at FROM approvals ORDER BY id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def has(self, scope: ApprovalScope, revision: str) -> bool:
        if not self.state_path.is_file():
            return False
        with self._connect() as connection:
            self._initialize(connection)
            row = connection.execute(
                "SELECT 1 FROM approvals WHERE scope = ? AND revision = ?",
                (scope.value, revision),
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
                approved_at TEXT NOT NULL,
                UNIQUE(scope, revision)
            )
            """
        )

    @staticmethod
    def _from_row(row: tuple[str, str, str]) -> ApprovalRecord:
        return ApprovalRecord(
            scope=ApprovalScope(row[0]),
            revision=row[1],
            approved_at=datetime.fromisoformat(row[2]),
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
    stage: str
    idempotency_key: str
    args: tuple[str, ...]
    state: StageState
    exit_code: int | None = None
    output: dict = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime | None = None


class StageAttemptLedger:
    """Durable, redacted attempt history for local and external pipeline stages."""

    def __init__(self, product_root: Path | str) -> None:
        self.state_path = Path(product_root) / "state.sqlite3"

    def start(self, stage: str, idempotency_key: str, args: tuple[str, ...]) -> StageAttempt:
        started_at = datetime.now(UTC)
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            cursor = connection.execute(
                """
                INSERT INTO stage_attempts(
                    stage, idempotency_key, args_json, state, started_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
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
                SELECT id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"stage attempt not found: {attempt_id}")
        return self._from_row(row)

    def latest(self, stage: str) -> StageAttempt | None:
        if not self.state_path.is_file():
            return None
        with sqlite3.connect(self.state_path) as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT id, stage, idempotency_key, args_json, state, exit_code,
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
                SELECT id, stage, idempotency_key, args_json, state, exit_code,
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
                SELECT id, stage, idempotency_key, args_json, state, exit_code,
                       output_json, started_at, finished_at
                FROM stage_attempts ORDER BY id
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_attempts(
                id INTEGER PRIMARY KEY,
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

    @staticmethod
    def _from_row(row) -> StageAttempt:
        return StageAttempt(
            id=row[0],
            stage=row[1],
            idempotency_key=row[2],
            args=tuple(json.loads(row[3])),
            state=StageState(row[4]),
            exit_code=row[5],
            output=json.loads(row[6] or "{}"),
            started_at=datetime.fromisoformat(row[7]),
            finished_at=datetime.fromisoformat(row[8]) if row[8] else None,
        )
