from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class ModelClient:
    """Vision caption, embeddings, LLM tool loop, ASR, translate, TTS."""

    def __init__(self) -> None:
        self.anthropic_key = settings.anthropic_api_key
        self.openai_key = settings.openai_api_key

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_key)

    @property
    def has_embeddings(self) -> bool:
        return bool(self.openai_key)

    def caption_image(self, image_path: Path) -> str:
        if self.anthropic_key:
            data = base64.standard_b64encode(image_path.read_bytes()).decode()
            payload = {
                "model": settings.vision_model,
                "max_tokens": 256,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": data,
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Describe this video frame in one concise sentence "
                                    "for search indexing."
                                ),
                            },
                        ],
                    }
                ],
            }
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
                parts = body.get("content") or []
                texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
                return " ".join(texts).strip() or "Frame captured."

        stem = image_path.stem
        return f"Mock caption for {stem} (set ANTHROPIC_API_KEY for vision)."

    def embed_text(self, text: str) -> list[float]:
        if self.openai_key:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self.openai_key}"},
                    json={"model": settings.embedding_model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                return list(data["data"][0]["embedding"])

        digest = hashlib.sha256(text.encode()).digest()
        vec = [((digest[i % len(digest)] / 255.0) * 2 - 1) for i in range(64)]
        return vec

    def llm_tool_loop(
        self,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        tool_handler: Any,
        max_rounds: int = 6,
    ) -> tuple[str, list[dict[str, Any]]]:
        trace: list[dict[str, Any]] = []
        if not self.anthropic_key:
            # Deterministic mock: run search_moments once if available
            for tool in tools:
                if tool.get("name") == "search_moments":
                    result = tool_handler("search_moments", {"query": user, "limit": 5})
                    trace.append({"tool": "search_moments", "result": result})
                    break
            snippets = trace[0]["result"] if trace else []
            if snippets:
                lines = [
                    f"- {s.get('moment_id')}: {s.get('caption', '')[:120]}"
                    for s in snippets[:3]
                ]
                return (
                    "Mock agent answer (set ANTHROPIC_API_KEY for full LLM loop):\n"
                    + "\n".join(lines),
                    trace,
                )
            return (
                "Mock agent answer (set ANTHROPIC_API_KEY). No indexed moments yet.",
                trace,
            )

        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        for _ in range(max_rounds):
            payload = {
                "model": settings.llm_model,
                "max_tokens": 1024,
                "system": system,
                "tools": tools,
                "messages": messages,
            }
            with httpx.Client(timeout=120) as client:
                resp = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.anthropic_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()

            stop = body.get("stop_reason")
            content = body.get("content") or []
            if stop == "end_turn":
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(texts).strip(), trace

            tool_uses = [c for c in content if c.get("type") == "tool_use"]
            if not tool_uses:
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return "\n".join(texts).strip() or "No response.", trace

            messages.append({"role": "assistant", "content": content})
            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                name = tu["name"]
                inp = tu.get("input") or {}
                out = tool_handler(name, inp)
                trace.append({"tool": name, "input": inp, "result": out})
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": json.dumps(out, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return "Agent stopped after max tool rounds.", trace

    def transcribe_wav(self, wav_path: Path) -> list[dict[str, Any]]:
        if settings.enable_whisper:
            try:
                from faster_whisper import WhisperModel

                model = WhisperModel(
                    settings.whisper_model_size,
                    device="cpu",
                    compute_type="int8",
                )
                segments, _ = model.transcribe(str(wav_path), beam_size=1)
                return [
                    {
                        "start": float(seg.start),
                        "end": float(seg.end),
                        "text": seg.text.strip(),
                    }
                    for seg in segments
                    if seg.text.strip()
                ]
            except Exception:
                pass
        return [
            {"start": 0.0, "end": 3.0, "text": "[mock transcript — install faster-whisper]"}
        ]

    def translate_segments(
        self,
        segments: list[dict[str, Any]],
        target_lang: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        flags: list[str] = []
        combined = " ".join(s["text"] for s in segments)
        flags.extend(self._compliance_scan(combined))

        if not self.anthropic_key:
            translated = [
                {**seg, "text": f"[{target_lang}] {seg['text']}"} for seg in segments
            ]
            return translated, flags

        prompt = (
            f"Translate the following transcript segments to {target_lang}. "
            "Return JSON array of objects with start, end, text (same timestamps).\n"
            f"{json.dumps(segments)}"
        )
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.llm_model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            body = resp.json()
        text = ""
        for part in body.get("content") or []:
            if part.get("type") == "text":
                text += part.get("text", "")
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                parsed = json.loads(match.group())
                flags.extend(self._compliance_scan(" ".join(p["text"] for p in parsed)))
                return parsed, flags
            except json.JSONDecodeError:
                pass
        translated = [
            {**seg, "text": f"[{target_lang}] {seg['text']}"} for seg in segments
        ]
        return translated, flags

    def _compliance_scan(self, text: str) -> list[str]:
        lower = text.lower()
        flags: list[str] = []
        if re.search(r"\b(nazi|swastika|hitler)\b", lower):
            flags.append("§86a-symbol-risk")
        if re.search(r"\b(personal data|email|phone number|address)\b", lower):
            flags.append("gdpr-pii-risk")
        if re.search(r"\b(blood|gore|violence)\b", lower):
            flags.append("fsk-violence-hint")
        return flags

    def synthesize_speech_de(self, text: str, out_path: Path) -> Path | None:
        if not settings.enable_tts:
            return None
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, settings.tts_voice_de)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            async def _save() -> None:
                await communicate.save(str(out_path))

            import asyncio

            asyncio.run(_save())
            return out_path
        except Exception:
            return None
