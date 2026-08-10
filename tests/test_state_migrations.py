import sqlite3

from agent_content_pipeline.state import ApprovalLedger, ApprovalScope, StageAttemptLedger


def test_legacy_approval_table_is_upgraded_without_losing_records(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE approvals(
                id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL,
                revision TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                UNIQUE(scope, revision)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO approvals(scope, revision, approved_at)
            VALUES ('article', 'v001', '2026-08-10T00:00:00+00:00')
            """
        )

    records = ApprovalLedger(tmp_path).list()

    assert len(records) == 1
    assert records[0].revision == "v001"
    assert records[0].content_digest is None


def test_legacy_stage_attempt_table_gets_stable_legacy_run_id(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE stage_attempts(
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
        connection.execute(
            """
            INSERT INTO stage_attempts(
                stage, idempotency_key, args_json, state, exit_code,
                output_json, started_at, finished_at
            ) VALUES (
                'video', 'video-key', '["video", "render"]', 'failed', 1,
                '{}', '2026-08-10T00:00:00+00:00', '2026-08-10T00:01:00+00:00'
            )
            """
        )

    attempts = StageAttemptLedger(tmp_path).list()

    assert attempts[0].run_id == "legacy-1"
    assert attempts[0].stage == "video"
