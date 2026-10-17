from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import video_dir
from app.models import ModelClient
from app.store import MomentStore


class AgentService:
    TOOL_DEFINITIONS = [
        {
            "name": "search_moments",
            "description": "Semantic search over indexed video moments.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query"],
            },
        },
        {
            "name": "query_detections",
            "description": "Filter YOLO detections by label and confidence.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "min_confidence": {"type": "number", "default": 0.35},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "ask_vision",
            "description": "Run vision caption on a specific frame path under the video.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "frame_path": {"type": "string"},
                },
                "required": ["frame_path"],
            },
        },
        {
            "name": "localize_moment",
            "description": "Return timing and paths for a moment id (for dubbing).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "moment_id": {"type": "string"},
                },
                "required": ["moment_id"],
            },
        },
    ]

    def __init__(self, models: ModelClient, store: MomentStore) -> None:
        self.models = models
        self.store = store

    def _handle_tool(self, vid: str, name: str, inp: dict[str, Any]) -> Any:
        if name == "search_moments":
            return self.store.search_moments(
                vid, inp.get("query", ""), int(inp.get("limit", 8))
            )
        if name == "query_detections":
            return self.store.query_detections(
                vid,
                label=inp.get("label"),
                min_confidence=float(inp.get("min_confidence", 0.35)),
                limit=int(inp.get("limit", 50)),
            )
        if name == "ask_vision":
            rel = inp.get("frame_path", "")
            path = video_dir(vid) / rel
            if not path.exists():
                return {"error": f"Frame not found: {rel}"}
            return {"caption": self.models.caption_image(path)}
        if name == "localize_moment":
            moment = self.store.get_moment(vid, inp.get("moment_id", ""))
            if not moment:
                return {"error": "moment not found"}
            return {
                "moment_id": moment["moment_id"],
                "start_sec": moment["start_sec"],
                "end_sec": moment["end_sec"],
                "frame_path": moment.get("frame_path"),
                "caption": moment.get("caption"),
            }
        return {"error": f"unknown tool {name}"}

    def ask(self, vid: str, question: str, max_tool_rounds: int = 6) -> tuple[str, list]:
        system = (
            "You are a video analyst agent. Use tools to search moments, "
            "inspect detections, and answer questions about the indexed video. "
            "Cite moment ids and timestamps when relevant."
        )

        def handler(name: str, inp: dict[str, Any]) -> Any:
            return self._handle_tool(vid, name, inp)

        return self.models.llm_tool_loop(
            system=system,
            user=f"Video id: {vid}\nQuestion: {question}",
            tools=self.TOOL_DEFINITIONS,
            tool_handler=handler,
            max_rounds=max_tool_rounds,
        )
