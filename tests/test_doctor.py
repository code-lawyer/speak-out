from pathlib import Path

from agent_content_pipeline.diagnostics import SystemDoctor


def test_doctor_reports_runtime_without_exposing_configured_secrets(tmp_path):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "doctor-secret"
""".lstrip(),
        encoding="utf-8",
    )
    chrome = tmp_path / "chrome.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for path in (chrome, ffmpeg, ffprobe):
        path.write_bytes(b"binary")

    report = SystemDoctor(
        project_root=tmp_path,
        which=lambda name: str(ffmpeg if name == "ffmpeg" else ffprobe),
        chrome_discover=lambda: chrome,
        git_ignore_check=lambda _root: True,
        chrome_version_read=lambda _path: 140,
    ).run()

    assert report.ok is True
    assert {check.name for check in report.checks if check.ok} >= {
        "python",
        "ffmpeg",
        "ffprobe",
        "chrome",
        "config",
        "git-ignore",
    }
    assert "doctor-secret" not in report.model_dump_json()


def test_doctor_rejects_local_secrets_directory_that_git_would_track(tmp_path):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        """
[website_wechat]
endpoint = "https://example.com/api/articles"
bearer_token = "never-print-this-secret"
""".lstrip(),
        encoding="utf-8",
    )
    chrome = tmp_path / "chrome.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for path in (chrome, ffmpeg, ffprobe):
        path.write_bytes(b"binary")

    report = SystemDoctor(
        project_root=tmp_path,
        which=lambda name: str(ffmpeg if name == "ffmpeg" else ffprobe),
        chrome_discover=lambda: chrome,
        git_ignore_check=lambda _root: False,
        chrome_version_read=lambda _path: 140,
    ).run()

    check = next(item for item in report.checks if item.name == "git-ignore")
    assert report.ok is False
    assert check.ok is False
    assert ".local/" in check.detail
    assert "never-print-this-secret" not in report.model_dump_json()


def test_doctor_rejects_an_installed_but_unsupported_chrome(tmp_path):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "secrets.toml").write_text(
        "[website_wechat]\nendpoint='https://example.com/api/articles'\nbearer_token='x'\n",
        encoding="utf-8",
    )
    chrome = tmp_path / "chrome.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    for path in (chrome, ffmpeg, ffprobe):
        path.write_bytes(b"binary")

    report = SystemDoctor(
        project_root=tmp_path,
        which=lambda name: str(ffmpeg if name == "ffmpeg" else ffprobe),
        chrome_discover=lambda: chrome,
        git_ignore_check=lambda _root: True,
        chrome_version_read=lambda _path: 115,
    ).run()

    check = next(item for item in report.checks if item.name == "chrome")
    assert report.ok is False
    assert check.ok is False
    assert "116 or newer" in check.detail
