"""Environment configuration for the investigation backend."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime settings loaded from the environment."""

    vss_url: str
    vss_username: str
    vss_password: str
    wandb_api_key: str = ""
    wandb_inference_url: str = "https://api.inference.wandb.ai/v1"
    wandb_model: str = "openai/gpt-oss-20b"
    request_timeout_sec: float = 60.0
    max_retries: int = 3
    min_similarity: float = 0.15
    ranking_concurrency: int = 4
    ticket_webhook_url: str = ""


def load_settings() -> Settings:
    """Load required VSS settings; raise ValueError if any are missing."""
    vss_url = (
        os.environ.get("VSS_URL") or os.environ.get("INGRESS_URL") or ""
    ).rstrip("/")
    username = os.environ.get("VSS_USERNAME") or os.environ.get("USERNAME") or ""
    password = os.environ.get("VSS_PASSWORD") or os.environ.get("PASSWORD") or ""
    missing = [
        name
        for name, val in (
            ("VSS_URL or INGRESS_URL", vss_url),
            ("VSS_USERNAME or USERNAME", username),
            ("VSS_PASSWORD or PASSWORD", password),
        )
        if not val
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    return Settings(
        vss_url=vss_url,
        vss_username=username,
        vss_password=password,
        wandb_api_key=os.environ.get("WANDB_API_KEY") or "",
        wandb_inference_url=(
            os.environ.get("WANDB_INFERENCE_URL") or "https://api.inference.wandb.ai/v1"
        ).rstrip("/"),
        wandb_model=os.environ.get("WANDB_MODEL") or "openai/gpt-oss-20b",
        ranking_concurrency=int(os.environ.get("RANKING_CONCURRENCY") or "4"),
        ticket_webhook_url=(os.environ.get("TICKET_WEBHOOK_URL") or "").strip(),
    )
