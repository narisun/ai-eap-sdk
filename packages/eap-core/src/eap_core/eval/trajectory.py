"""Trajectory recording for eval and audit."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request, Response


class Step(BaseModel):
    role: str
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


class Trajectory(BaseModel):
    request_id: str
    steps: list[Step] = Field(default_factory=list)
    final_answer: str = ""
    retrieved_contexts: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class TrajectoryRecorder(PassthroughMiddleware):
    """Middleware that captures Trajectory per request and writes JSONL."""

    name = "trajectory_recorder"

    def __init__(self, out_path: Path | str | None = None) -> None:
        self._out_path = Path(out_path) if out_path else None
        self._buffer: list[Trajectory] = []

    async def on_request(self, req: Request, ctx: Context) -> Request:
        ctx.metadata.setdefault("eval.input_text", _flatten(req))
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        traj = Trajectory(
            request_id=ctx.request_id,
            steps=[
                Step(
                    role="assistant",
                    text=resp.text,
                    input_tokens=resp.usage.get("input_tokens", 0),
                    output_tokens=resp.usage.get("output_tokens", 0),
                    model=ctx.metadata.get("gen_ai.request.model"),
                )
            ],
            final_answer=resp.text,
            retrieved_contexts=list(ctx.metadata.get("retrieved_contexts", [])),
            extra={"input_text": ctx.metadata.get("eval.input_text", "")},
        )
        self._buffer.append(traj)
        if self._out_path is not None:
            self._out_path.parent.mkdir(parents=True, exist_ok=True)
            with self._out_path.open("a") as f:
                f.write(traj.model_dump_json() + "\n")
        return resp

    @property
    def trajectories(self) -> list[Trajectory]:
        return list(self._buffer)


def _flatten(req: Request) -> str:
    parts: list[str] = []
    for m in req.messages:
        parts.append(m.content if isinstance(m.content, str) else str(m.content))
    return "\n".join(parts)
