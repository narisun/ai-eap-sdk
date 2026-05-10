"""Eval runner — drives a golden dataset through an agent and scores trajectories."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from eap_core.eval.faithfulness import FaithfulnessResult
from eap_core.eval.trajectory import Trajectory


class EvalCase(BaseModel):
    id: str
    input: str
    expected_contexts: list[str] = Field(default_factory=list)
    expected_answer_substrings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseResult(BaseModel):
    case_id: str
    trajectory: Trajectory
    scores: dict[str, FaithfulnessResult] = Field(default_factory=dict)
    passed: bool = True
    notes: str = ""


class EvalReport(BaseModel):
    cases: list[CaseResult] = Field(default_factory=list)
    aggregate: dict[str, float] = Field(default_factory=dict)
    threshold: float = 0.7
    passed_count: int = 0
    failed_count: int = 0


AgentFn = Callable[[EvalCase], Awaitable[Trajectory]]


class _ScorerProto(Protocol):
    name: str

    async def score(self, traj: Trajectory) -> FaithfulnessResult: ...


class EvalRunner:
    def __init__(
        self,
        agent: AgentFn,
        scorers: list[_ScorerProto],
        threshold: float = 0.7,
    ) -> None:
        self._agent = agent
        self._scorers = scorers
        self._threshold = threshold

    @staticmethod
    def load_dataset(path: Path | str) -> list[EvalCase]:
        data = json.loads(Path(path).read_text())
        return [EvalCase(**item) for item in data]

    async def run(self, cases: list[EvalCase]) -> EvalReport:
        results: list[CaseResult] = []
        totals: dict[str, list[float]] = {}
        for case in cases:
            traj = await self._agent(case)
            scores: dict[str, FaithfulnessResult] = {}
            passed_case = True
            for scorer in self._scorers:
                result = await scorer.score(traj)
                scores[scorer.name] = result
                totals.setdefault(scorer.name, []).append(result.score)
                if result.score < self._threshold:
                    passed_case = False
            results.append(
                CaseResult(
                    case_id=case.id,
                    trajectory=traj,
                    scores=scores,
                    passed=passed_case,
                )
            )
        aggregate = {name: sum(vals) / len(vals) for name, vals in totals.items()}
        return EvalReport(
            cases=results,
            aggregate=aggregate,
            threshold=self._threshold,
            passed_count=sum(1 for r in results if r.passed),
            failed_count=sum(1 for r in results if not r.passed),
        )
