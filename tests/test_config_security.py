import subprocess

import pytest

from agent_content_pipeline.config import LocalConfig


def test_config_refuses_to_load_a_secret_file_tracked_by_git(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    local = tmp_path / ".local"
    local.mkdir()
    secrets = local / "secrets.toml"
    secrets.write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "must-not-leak"
""".lstrip(),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", ".local/secrets.toml"],
        check=True,
        capture_output=True,
    )

    with pytest.raises(RuntimeError) as error:
        LocalConfig(tmp_path).load()

    assert "tracked by Git" in str(error.value)
    assert "must-not-leak" not in str(error.value)


@pytest.mark.parametrize(
    ("endpoint", "token"),
    (
        ("https://example.com/api/articles", "configured-token"),
        ("https://hillward.top/api/articles", ""),
        ("http://hillward.top/api/articles", "configured-token"),
        ("https://hillward.top/not-the-article-route", "configured-token"),
    ),
)
def test_config_refuses_unready_article_publication_credentials(
    tmp_path,
    endpoint,
    token,
):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        (
            "[website_wechat]\n"
            f'endpoint = "{endpoint}"\n'
            f'bearer_token = "{token}"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        LocalConfig(tmp_path).load()
