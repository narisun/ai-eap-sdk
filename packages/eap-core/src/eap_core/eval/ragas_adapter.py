"""Ragas dataset adapter — convert Trajectory list to Ragas-friendly rows."""
from __future__ import annotations

from typing import Any

from eap_core.eval.trajectory import Trajectory


def to_ragas_dataset(trajectories: list[Trajectory]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in trajectories:
        question = t.extra.get("input_text", "") if t.extra else ""
        rows.append({
            "question": question,
            "answer": t.final_answer,
            "contexts": list(t.retrieved_contexts),
            "request_id": t.request_id,
        })
    return rows
