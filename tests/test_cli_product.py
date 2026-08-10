import json
from datetime import date

from typer.testing import CliRunner

from agent_content_pipeline.cli import app
from agent_content_pipeline.workspace import ProductCreateRequest, ProductWorkspace


def test_cli_creates_a_product_and_reports_machine_readable_result(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "product",
            "create",
            "--title",
            "技术加速主义的残酷本质",
            "--slug",
            "technology-acceleration",
            "--date",
            "2026-08-10",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["product"]["slug"] == "technology-acceleration"
    assert payload["product"]["root"] == str(
        tmp_path / "2026-08-10-technology-acceleration"
    )


def test_cli_initializes_one_editable_secret_file_from_the_project_template(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "secrets.example.toml").write_text(
        '[website_wechat]\nendpoint = "https://example.com/api/articles"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["config", "init", "--project-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    secrets_path = tmp_path / ".local" / "secrets.toml"
    assert payload == {"ok": True, "secretsPath": str(secrets_path)}
    assert secrets_path.read_text(encoding="utf-8") == (
        '[website_wechat]\nendpoint = "https://example.com/api/articles"\n'
    )


def test_cli_migrates_legacy_vps_credentials_without_printing_the_secret(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "secrets.example.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = ""
request_timeout_seconds = 30
""".lstrip(),
        encoding="utf-8",
    )
    legacy = tmp_path / "push.mjs"
    legacy.write_text(
        """
const SITE_URL = "https://hillward.top";
const SUBMIT_SECRET = "legacy-private-token";
const REQUEST_TIMEOUT_MS = 45_000;
""".lstrip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "config",
            "migrate-legacy-article",
            "--project-root",
            str(tmp_path),
            "--legacy-push",
            str(legacy),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "legacy-private-token" not in result.stdout
    secret_text = (tmp_path / ".local" / "secrets.toml").read_text("utf-8")
    assert 'endpoint = "https://hillward.top/api/articles"' in secret_text
    assert 'bearer_token = "legacy-private-token"' in secret_text
    assert "request_timeout_seconds = 45" in secret_text


def test_cli_reports_a_product_after_a_fresh_process_load(tmp_path):
    created = ProductWorkspace(tmp_path).create(
        ProductCreateRequest(
            title="技术加速主义的残酷本质",
            slug="technology-acceleration",
            created_on=date(2026, 8, 10),
        )
    )

    result = CliRunner().invoke(
        app,
        ["product", "status", "--product", str(created.root), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["product"] == {
        "id": created.manifest.product_id,
        "title": "技术加速主义的残酷本质",
        "slug": "technology-acceleration",
        "createdOn": "2026-08-10",
        "root": str(created.root),
    }


def test_cli_records_approval_for_one_exact_artifact_revision(tmp_path):
    product = ProductWorkspace(tmp_path).create(
        ProductCreateRequest(
            title="测试文章",
            slug="approval-test",
            created_on=date(2026, 8, 10),
        )
    )

    recorded = CliRunner().invoke(
        app,
        [
            "approval",
            "record",
            "--product",
            str(product.root),
            "--scope",
            "article",
            "--revision",
            "v001",
            "--confirmed-by-user",
            "--json",
        ],
    )

    assert recorded.exit_code == 0
    assert json.loads(recorded.stdout) == {
        "ok": True,
        "approval": {"scope": "article", "revision": "v001"},
    }

    status = CliRunner().invoke(
        app,
        ["product", "status", "--product", str(product.root), "--json"],
    )
    assert json.loads(status.stdout)["approvals"] == [
        {"scope": "article", "revision": "v001"}
    ]
