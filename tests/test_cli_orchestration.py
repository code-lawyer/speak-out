import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.state import StageAttemptLedger, StageState
from agent_content_pipeline.workspace import ProductCreateRequest, ProductWorkspace


def create_product(tmp_path):
    return ProductWorkspace(tmp_path / "workspace").create(
        ProductCreateRequest(
            title="编排测试",
            slug="orchestration-test",
            created_on=date(2026, 8, 10),
        )
    )


def test_run_cli_is_dry_by_default_and_builds_independent_exact_commands(tmp_path):
    product = create_product(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--stage",
            "article",
            "--stage",
            "video",
            "--stage",
            "social:bilibili",
            "--article-revision",
            "v001",
            "--cover-revision",
            "v002",
            "--script-revision",
            "v003",
            "--material-revision",
            "v004",
            "--allow-edge-tts-data-transfer",
            "--video-revision",
            "v005",
            "--copy-revision",
            "v006",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert payload["run_id"]
    assert [item["stage"] for item in payload["stages"]] == [
        "article",
        "video",
        "social:bilibili",
    ]
    assert all(item["state"] == "planned" for item in payload["stages"])
    assert "--json" in payload["stages"][0]["args"]
    assert "--execute" in payload["stages"][0]["args"]
    assert "--allow-edge-tts-data-transfer" in payload["stages"][1]["args"]
    assert "--execute" in payload["stages"][2]["args"]
    assert StageAttemptLedger(product.root).list() == []


def test_retry_cli_dry_runs_only_latest_failed_or_waiting_stage(tmp_path):
    product = create_product(tmp_path)
    ledger = StageAttemptLedger(product.root)
    attempt = ledger.start(
        "social:douyin",
        "exact-social-key",
        (
            "social",
            "publish",
            "--project-root",
            str(tmp_path),
            "--product",
            str(product.root),
            "--platform",
            "douyin",
            "--video-revision",
            "v001",
            "--copy-revision",
            "v001",
            "--json",
        ),
    )
    ledger.finish(
        attempt.id,
        StageState.WAITING_FOR_USER,
        3,
        {"state": "waiting_for_user"},
    )

    result = CliRunner().invoke(
        app,
        [
            "retry",
            "--product",
            str(product.root),
            "--stage",
            "social:douyin",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert payload["stages"][0]["stage"] == "social:douyin"
    assert payload["stages"][0]["output"]["retryOfAttempt"] == attempt.id
    assert len(ledger.list()) == 1


def test_retry_cli_refuses_unknown_state(tmp_path):
    product = create_product(tmp_path)
    ledger = StageAttemptLedger(product.root)
    attempt = ledger.start("article", "article-key", ("article", "publish", "--json"))
    ledger.finish(attempt.id, StageState.UNKNOWN, 1, {"state": "unknown"})

    result = CliRunner().invoke(
        app,
        [
            "retry",
            "--product",
            str(product.root),
            "--stage",
            "article",
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "not safe to retry" in result.output


def test_product_status_includes_stage_attempt_history(tmp_path):
    product = create_product(tmp_path)
    ledger = StageAttemptLedger(product.root)
    attempt = ledger.start("video", "video-key", ("video", "render", "--json"))
    ledger.finish(attempt.id, StageState.FAILED, 1, {"error": "ffmpeg"})

    result = CliRunner().invoke(
        app,
        ["product", "status", "--product", str(product.root), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["stageAttempts"][0]["stage"] == "video"
    assert payload["stageAttempts"][0]["state"] == "failed"
    assert payload["stageAttempts"][0]["runId"]


def test_reconcile_cli_requires_confirmation_and_unblocks_unknown_as_absent(tmp_path):
    product = create_product(tmp_path)
    ledger = StageAttemptLedger(product.root)
    unknown = ledger.start(
        "article",
        "article-key",
        ("article", "publish", "--json"),
    )
    ledger.finish(unknown.id, StageState.UNKNOWN, 1, {"state": "unknown"})

    preview = CliRunner().invoke(
        app,
        [
            "reconcile",
            "--product",
            str(product.root),
            "--stage",
            "article",
            "--outcome",
            "absent",
            "--evidence",
            "VPS and website checked; article is absent.",
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["mode"] == "dry-run"
    assert len(ledger.list()) == 1

    denied = CliRunner().invoke(
        app,
        [
            "reconcile",
            "--product",
            str(product.root),
            "--stage",
            "article",
            "--outcome",
            "absent",
            "--evidence",
            "VPS and website checked; article is absent.",
            "--execute",
            "--json",
        ],
    )
    assert denied.exit_code != 0
    assert "--confirmed-by-user" in denied.output

    applied = CliRunner().invoke(
        app,
        [
            "reconcile",
            "--product",
            str(product.root),
            "--stage",
            "article",
            "--outcome",
            "absent",
            "--evidence",
            "VPS and website checked; article is absent.",
            "--execute",
            "--confirmed-by-user",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["stages"][0]["state"] == "failed"
    assert ledger.latest("article").output["reconciliation"]["evidence"].startswith("VPS")
