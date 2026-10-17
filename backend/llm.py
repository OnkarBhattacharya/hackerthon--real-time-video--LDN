"""W&B serverless inference client (OpenAI-compatible chat)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from backend.config import Settings, load_settings

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """Inference call failed."""


class LlmClient:
    """Chat completions against Weights & Biases Inference."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.settings.wandb_api_key)

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 800,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Call the model and return a parsed JSON object.

        Uses response_format json_object when possible; raises LlmError on failure.
        """
        if not self.available:
            raise LlmError("WANDB_API_KEY not set")
        content = self._complete(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
        )
        try:
            return _parse_json_object(content)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("llm.json_parse_failed; attempting repair")
            repaired = self._complete(
                system=(
                    "Fix the following into a single valid JSON object. "
                    "Output JSON only, no markdown."
                ),
                user=f"Invalid output to repair:\n{content}\n\nError: {exc}",
                max_tokens=max_tokens,
                temperature=0.0,
                json_mode=True,
            )
            return _parse_json_object(repaired)

    def _complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        url = f"{self.settings.wandb_inference_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self.settings.wandb_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.settings.wandb_api_key}",
            "Content-Type": "application/json",
        }
        last_err: Exception | None = None
        for attempt in range(self.settings.max_retries):
            try:
                log.info(
                    "llm.chat model=%s attempt=%s json_mode=%s",
                    self.settings.wandb_model,
                    attempt + 1,
                    json_mode,
                )
                r = self._session.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=self.settings.request_timeout_sec,
                )
                if r.status_code in (429, 500, 502, 503, 504) and attempt + 1 < self.settings.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                if r.status_code >= 400:
                    raise LlmError(f"chat HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content")
                if not content:
                    raise LlmError("empty model content")
                return str(content)
            except (requests.RequestException, LlmError, KeyError, IndexError) as exc:
                last_err = exc
                if attempt + 1 >= self.settings.max_retries:
                    break
                time.sleep(0.5 * (2**attempt))
        raise LlmError(f"chat failed after retries: {last_err}")


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data
