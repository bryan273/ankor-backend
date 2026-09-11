"""One streamed turn, recorded in a shape assertions can read."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class Turn:
    """Everything one streamed turn produced, in a shape assertions can read."""

    def __init__(self) -> None:
        self.events: List[tuple[str, Dict[str, Any]]] = []
        self.text = ""
        self.blocks: List[Dict[str, Any]] = []
        self.tools: List[str] = []
        self.tool_results: List[Dict[str, Any]] = []
        self.citations: List[Dict[str, Any]] = []
        self.suggestions: List[str] = []
        self.stages_started: List[str] = []
        self.stages_closed: List[str] = []
        self.emotion: Optional[str] = None
        self.urgency: Dict[str, Any] = {}
        self.guard_hits: List[str] = []
        self.session_id: Optional[str] = None
        self.complete: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, Any]] = None
        self.usage: Dict[str, Any] = {}
        self.ms = 0

    def feed(self, name: str, data: Dict[str, Any]) -> None:
        self.events.append((name, data))
        if name == "status":
            self.session_id = data.get("session_id")
        elif name == "stage_start":
            self.stages_started.append(data["stage_id"])
        elif name in ("stage_complete", "stage_error"):
            self.stages_closed.append(data["stage_id"])
        elif name == "emotion":
            self.emotion = data.get("emotion")
            self.urgency = data.get("urgency") or {}
        elif name == "tool_call":
            self.tools.append(data["tool"])
        elif name == "tool_result":
            self.tool_results.append(data)
        elif name == "content_delta":
            self.text += data.get("delta", "")
        elif name == "content_reset":
            self.text = ""
        elif name == "ui_block":
            self.blocks.append(data)
        elif name == "citation":
            self.citations.append(data)
        elif name == "suggestions":
            self.suggestions = [i["text"] for i in data.get("items", [])]
        elif name == "usage":
            self.usage = data
        elif name == "complete":
            self.complete = data
            self.guard_hits = data.get("guard_hits", [])
        elif name == "error":
            self.error = data

    @property
    def block_types(self) -> List[str]:
        return [b["type"] for b in self.blocks]

    @property
    def unclosed_stages(self) -> List[str]:
        closed = list(self.stages_closed)
        out = []
        for s in self.stages_started:
            if s in closed:
                closed.remove(s)
            else:
                out.append(s)
        return out

    def evidence(self) -> str:
        """What the tools reported, for the judge to grade grounding against.

        Only summaries are available client-side — the full results stay server-side —
        but a summary is enough to tell an invented dealer from a real one.
        """
        lines = [f"{r.get('call_id', '')} {r.get('summary', '')}".strip()
                 for r in self.tool_results if r.get("ok")]
        named = [f"- {tool}: {line}" for tool, line in zip(self.tools, lines)]
        return "\n".join(named) or "(no tools were called)"

    def block(self, block_type: str) -> Optional[Dict[str, Any]]:
        for b in self.blocks:
            if b["type"] == block_type:
                return b
        return None


