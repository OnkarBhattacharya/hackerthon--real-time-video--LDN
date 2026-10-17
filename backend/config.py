"""Environment configuration. Required values are checked when a call needs them."""

from __future__ import annotations

import os
from pathlib import Path


class ConfigError(Exception):
    """A required environment variable is missing."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


# Stage 0 did not find a frame or speech signal. See NOTES.md.
CROP_AVAILABLE = False
AUDIO_AVAILABLE = False


class Settings:
    """Read process environment variables. Secrets are never logged by callers."""

    def use_fixture(self) -> bool:
        """Return True when local sample segments should replace the live archive."""
        return os.environ.get("VSS_USE_FIXTURE", "").strip().lower() in {"1", "true", "yes"}

    def snapshot_path(self) -> Path:
        """Return the JSON snapshot path for the last successful investigation."""
        return Path(os.environ.get("SNAPSHOT_PATH", "data/last_run.json"))

    def port(self) -> int:
        """Return the HTTP port."""
        raw = os.environ.get("PORT", "8080").strip() or "8080"
        return int(raw)

    def _first(self, *names: str) -> str | None:
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return None

    def missing_vss(self) -> list[str]:
        """Return the names of missing login variables. Empty when login can run."""
        missing: list[str] = []
        if not self._first("VSS_URL", "INGRESS_URL"):
            missing.append("VSS_URL or INGRESS_URL")
        if not self._first("VSS_USERNAME", "USERNAME"):
            missing.append("VSS_USERNAME or USERNAME")
        if not self._first("VSS_PASSWORD", "PASSWORD"):
            missing.append("VSS_PASSWORD or PASSWORD")
        return missing

    def vss_configured(self) -> bool:
        """Return True when the retrieval login variables are all set."""
        return not self.missing_vss()

    def require_vss(self) -> tuple[str, str, str]:
        """Return the base URL, username, and password, or raise ConfigError."""
        base = self._first("VSS_URL", "INGRESS_URL")
        user = self._first("VSS_USERNAME", "USERNAME")
        password = self._first("VSS_PASSWORD", "PASSWORD")
        if not base:
            raise ConfigError("VSS_URL or INGRESS_URL")
        if not user:
            raise ConfigError("VSS_USERNAME or USERNAME")
        if not password:
            raise ConfigError("VSS_PASSWORD or PASSWORD")
        return base.rstrip("/"), user, password

    def wandb_api_key(self) -> str | None:
        """Return the W&B inference key when it is set."""
        return self._first("WANDB_API_KEY")

    def wandb_base_url(self) -> str:
        """Return the OpenAI-compatible inference base URL."""
        return (
            self._first("WANDB_BASE_URL") or "https://api.inference.wandb.ai/v1"
        ).rstrip("/")

    def wandb_model(self) -> str | None:
        """Return a caller-selected model id, if one was configured."""
        return self._first("WANDB_MODEL")

    def ticket_webhook_url(self) -> str | None:
        """Return the ticket webhook URL, or None when tickets are download-only."""
        return self._first("TICKET_WEBHOOK_URL")

    def cameras_not_indexed(self) -> set[str]:
        """Return camera ids Stage 0 marked as not indexed."""
        raw = os.environ.get("CAMERAS_NOT_INDEXED", "")
        return {part.strip() for part in raw.split(",") if part.strip()}


settings = Settings()
