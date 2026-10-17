"""Minimal FastAPI entrypoint (Stage 1: health only)."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Near-miss investigation", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"ok": True, "service": "near-miss", "stage": "1-data-layer"}
