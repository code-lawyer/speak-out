from __future__ import annotations

import sqlite3
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
