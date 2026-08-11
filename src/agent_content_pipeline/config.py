from __future__ import annotations

import shutil
import json
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


_PLACEHOLDER_HOSTS = {
    "example.com",
    "www.example.com",
    "localhost",
}


def validate_website_wechat_credentials(endpoint: str, bearer_token: str) -> tuple[str, str]:
    """Validate the fixed-IP VPS seam without exposing either value."""

    normalized_endpoint = endpoint.strip()
    parsed = urlsplit(normalized_endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() in _PLACEHOLDER_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api/articles"
    ):
        raise ValueError(
            "website/WeChat endpoint must be a configured HTTPS /api/articles VPS route"
        )
    normalized_token = bearer_token.strip()
    if not normalized_token:
        raise ValueError("website/WeChat bearer token is missing")
    return normalized_endpoint, normalized_token


class WebsiteWechatSecrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str = Field(min_length=1)
    bearer_token: SecretStr
    request_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        endpoint, _ = validate_website_wechat_credentials(value, "validation-placeholder")
        return endpoint

    @field_validator("bearer_token")
    @classmethod
    def validate_bearer_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token:
            raise ValueError("website/WeChat bearer token is missing")
        return SecretStr(token)


class PexelsSecrets(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key: SecretStr = SecretStr("")


class TtsSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    voice: str = "zh-CN-YunxiNeural"
    request_timeout_seconds: int = Field(default=120, ge=1, le=600)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    chrome_path: str = ""


class LocalSecrets(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    website_wechat: WebsiteWechatSecrets
    pexels: PexelsSecrets = PexelsSecrets()
    tts: TtsSettings = TtsSettings()
    browser: BrowserSettings = BrowserSettings()


class LocalConfig:
    """Own the user-editable local configuration layout."""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root)

    @property
    def secrets_path(self) -> Path:
        return self.project_root / ".local" / "secrets.toml"

    @property
    def template_path(self) -> Path:
        return self.project_root / "config" / "secrets.example.toml"

    def initialize(self) -> Path:
        if not self.template_path.is_file():
            raise FileNotFoundError(f"secret template not found: {self.template_path}")
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        with self.template_path.open("rb") as source, self.secrets_path.open("xb") as target:
            shutil.copyfileobj(source, target)
        return self.secrets_path

    def load(self) -> LocalSecrets:
        self._assert_secret_file_is_not_tracked()
        with self.secrets_path.open("rb") as handle:
            return LocalSecrets.model_validate(tomllib.load(handle))

    def _assert_secret_file_is_not_tracked(self) -> None:
        if not (self.project_root / ".git").exists() or not self.secrets_path.is_file():
            return
        relative = self.secrets_path.relative_to(self.project_root)
        completed = subprocess.run(
            (
                "git",
                "-c",
                f"safe.directory={self.project_root.resolve().as_posix()}",
                "-C",
                str(self.project_root),
                "ls-files",
                "--error-unmatch",
                relative.as_posix(),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            raise RuntimeError(
                ".local/secrets.toml is tracked by Git; remove it from the index before continuing"
            )

    def migrate_legacy_article_publisher(self, legacy_push: Path) -> Path:
        source = legacy_push.read_text(encoding="utf-8")
        site_match = re.search(r'const\s+SITE_URL\s*=\s*["\']([^"\']+)["\']', source)
        secret_match = re.search(
            r'const\s+SUBMIT_SECRET\s*=\s*["\']([^"\']+)["\']',
            source,
        )
        timeout_match = re.search(r"const\s+REQUEST_TIMEOUT_MS\s*=\s*([0-9_]+)", source)
        if site_match is None or secret_match is None:
            raise ValueError("legacy push script does not contain the expected VPS credentials")
        if not self.secrets_path.is_file():
            self.initialize()
        text = self.secrets_path.read_text(encoding="utf-8")
        endpoint = site_match.group(1).rstrip("/") + "/api/articles"
        timeout_seconds = (
            int(timeout_match.group(1).replace("_", "")) // 1000
            if timeout_match
            else 30
        )
        replacements = {
            "endpoint": json.dumps(endpoint),
            "bearer_token": json.dumps(secret_match.group(1)),
            "request_timeout_seconds": str(timeout_seconds),
        }
        for key, value in replacements.items():
            pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
            if re.search(pattern, text):
                text = re.sub(pattern, f"{key} = {value}", text, count=1)
            else:
                section_match = re.search(r"(?m)^\[website_wechat\]\s*$", text)
                if section_match is None:
                    text = f"[website_wechat]\n{key} = {value}\n\n" + text
                else:
                    insertion = section_match.end()
                    text = text[:insertion] + f"\n{key} = {value}" + text[insertion:]
        self.secrets_path.write_text(text, encoding="utf-8")
        return self.secrets_path
