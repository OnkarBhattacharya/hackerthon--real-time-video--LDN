"""W&B inference client. Callers fall back when this is not configured."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class LlmError(Exception):
    """The inference endpoint could not complete a request."""


class LlmClient:
    """OpenAI-compatible chat client with a timeout on every call."""

    def __init__(self) -> None:
        self._model: str | None = None

    def configured(self) -> bool:
        """Return True when an API key is present."""
        return settings.wandb_api_key() is not None

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        """Send one chat request and return the assistant text."""
        if not self.configured():
            raise LlmError("WANDB_API_KEY is not set")
        model = self._resolve_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        response = self._post(payload)
        if json_mode and response.status_code == 400:
            logger.info("llm chat model=%s json_mode=rejected", model)
            payload.pop("response_format", None)
            response = self._post(payload)
        elapsed = time.perf_counter() - started
        logger.info(
            "llm chat model=%s status=%s elapsed=%.2f",
            model,
            response.status_code,
            elapsed,
        )
        if response.status_code >= 400:
            raise LlmError(f"inference failed ({response.status_code})")
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("inference returned no message") from exc
        if not isinstance(content, str) or not content.strip():
            raise LlmError("inference returned an empty message")
        return content

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        key = settings.wandb_api_key() or ""
        url = settings.wandb_base_url() + "/chat/completions"
        try:
            return httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            logger.info("llm chat model=%s status=error", payload.get("model"))
            raise LlmError("inference did not respond") from exc

    def _resolve_model(self) -> str:
        chosen = settings.wandb_model()
        if chosen:
            return chosen
        if self._model:
            return self._model
        url = settings.wandb_base_url() + "/models"
        key = settings.wandb_api_key() or ""
        started = time.perf_counter()
        try:
            response = httpx.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            logger.info("llm models status=error elapsed=%.2f", time.perf_counter() - started)
            raise LlmError("could not list models") from exc
        logger.info(
            "llm models status=%s elapsed=%.2f",
            response.status_code,
            time.perf_counter() - started,
        )
        if response.status_code >= 400:
            raise LlmError(f"could not list models ({response.status_code})")
        model_id = _pick_model(response.json())
        if not model_id:
            raise LlmError("no instruction-following model was listed")
        self._model = model_id
        return model_id


def _pick_model(payload: dict[str, Any]) -> str | None:
    raw_items = payload.get("data") or payload.get("models") or []
    ids: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    preferred = [
        model_id
        for model_id in ids
        if "embed" not in model_id.lower()
        and any(token in model_id.lower() for token in ("instruct", "chat", "reason"))
    ]
    if preferred:
        return preferred[0]
    for model_id in ids:
        if "embed" not in model_id.lower():
            return model_id
    return None


llm = LlmClient()
