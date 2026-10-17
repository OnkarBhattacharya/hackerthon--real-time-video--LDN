from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import settings


class YoloService:
    """Ultralytics YOLO in-process (optional)."""

    def __init__(self) -> None:
        self._model = None
        self._load_error: str | None = None
        if settings.enable_yolo:
            try:
                from ultralytics import YOLO

                self._model = YOLO(settings.yolo_model)
            except Exception as exc:  # pragma: no cover - optional dep
                self._load_error = str(exc)
                self._model = None

    @property
    def available(self) -> bool:
        return self._model is not None

    def detect_frame(self, frame_path: Path) -> list[dict[str, Any]]:
        if self._model is None:
            return [
                {
                    "label": "mock-person",
                    "confidence": 0.5,
                    "bbox": [0.1, 0.1, 0.4, 0.9],
                    "source": "mock",
                    "note": self._load_error or "ultralytics not installed",
                }
            ]

        results = self._model(str(frame_path), verbose=False)
        detections: list[dict[str, Any]] = []
        for r in results:
            names = r.names or {}
            if r.boxes is None:
                continue
            for box in r.boxes:
                conf = float(box.conf[0])
                if conf < settings.yolo_confidence:
                    continue
                cls_id = int(box.cls[0])
                xyxy = [float(x) for x in box.xyxy[0].tolist()]
                detections.append(
                    {
                        "label": names.get(cls_id, str(cls_id)),
                        "confidence": conf,
                        "bbox": xyxy,
                        "source": "yolo",
                    }
                )
        return detections
