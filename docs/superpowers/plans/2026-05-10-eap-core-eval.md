# EAP-Core Eval Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the evaluation framework so agents can be measured against a golden dataset. Captures trajectories from the middleware chain, scores answers for **Faithfulness** via a swappable Judge, drives a runner that consumes a JSON dataset, and emits JSON/HTML/JUnit reports. Optional Ragas adapter under the `[eval]` extra.

**Architecture:** `TrajectoryRecorder` middleware writes one JSONL record per request (reuses OTel attributes from Plan 1's `ObservabilityMiddleware` — no parallel observability stack). `FaithfulnessScorer` takes a `Judge` Protocol; we ship `DeterministicJudge` (substring overlap, used in tests) and `LLMJudge` (wraps `EnterpriseLLM` with eval middlewares stripped to avoid recursion). `EvalRunner` drives a golden-set JSON file through a user-provided agent function, scores the trajectories, and produces an `EvalReport`. The CLI's `eap eval` command (Plan 4) is a thin wrapper around `EvalRunner`.

**Tech Stack:** Python 3.11+, Pydantic v2. Optional: Ragas (`[eval]` extra).

**Spec reference:** `docs/superpowers/specs/2026-05-10-eap-core-design.md` §12.
**Predecessors:** Plan 1 (foundation), Plan 2 (standards) — both must be in place.

---

## File Structure

```
packages/eap-core/src/eap_core/eval/
├── __init__.py
├── trajectory.py       # Trajectory, Step, TrajectoryRecorder middleware
├── faithfulness.py     # Judge Protocol, DeterministicJudge, LLMJudge, FaithfulnessScorer
├── runner.py           # EvalCase, EvalReport, EvalRunner
├── reports.py          # JSON / HTML / JUnit emitters
└── ragas_adapter.py    # Optional Ragas Dataset converter ([eval] extra)

packages/eap-core/tests/
├── test_eval_trajectory.py
├── test_eval_faithfulness.py
├── test_eval_runner.py
├── test_eval_reports.py
└── extras/
    └── test_ragas_adapter.py
```

---

## Task 1: Trajectory dataclass + TrajectoryRecorder middleware

**Files:**
- Create: `packages/eap-core/src/eap_core/eval/__init__.py`
- Create: `packages/eap-core/src/eap_core/eval/trajectory.py`
- Create: `packages/eap-core/tests/test_eval_trajectory.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/eap-core/tests/test_eval_trajectory.py
import json

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.eval.trajectory import Step, Trajectory, TrajectoryRecorder
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware


PERMIT_ALL = {
    "version": "1",
    "rules": [{"id": "permit", "effect": "permit", "principal": "*", "action": "*", "resource": "*"}],
}


def test_trajectory_step_carries_role_and_text():
    step = Step(role="assistant", text="hi", input_tokens=1, output_tokens=2)
    assert step.role == "assistant"
    assert step.input_tokens == 1


def test_trajectory_serializes_to_jsonable_dict():
    traj = Trajectory(
        request_id="r1",
        steps=[Step(role="assistant", text="ok", input_tokens=3, output_tokens=1)],
        final_answer="ok",
        retrieved_contexts=["c1"],
    )
    d = traj.model_dump()
    assert d["request_id"] == "r1"
    assert d["steps"][0]["text"] == "ok"


async def test_recorder_writes_jsonl_per_request(tmp_path):
    out = tmp_path / "traces.jsonl"
    recorder = TrajectoryRecorder(out_path=out)
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            ObservabilityMiddleware(),
            PolicyMiddleware(JsonPolicyEvaluator(PERMIT_ALL)),
            recorder,
        ],
    )
    await client.generate_text("hello world")
    await client.generate_text("another prompt")

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["request_id"]
    assert rec["final_answer"]


async def test_recorder_collects_retrieved_contexts_from_ctx(tmp_path):
    """If a middleware stashes contexts in ctx.metadata['retrieved_contexts'],
    the recorder should pick them up."""
    out = tmp_path / "traces.jsonl"
    recorder = TrajectoryRecorder(out_path=out)

    # Simple middleware that injects contexts before the recorder runs.
    from eap_core.middleware.base import PassthroughMiddleware
    from eap_core.types import Context, Request, Response

    class CtxStuffer(PassthroughMiddleware):
        name = "stuffer"
        async def on_request(self, req: Request, ctx: Context) -> Request:
            ctx.metadata["retrieved_contexts"] = ["doc:1 says X", "doc:2 says Y"]
            return req

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            CtxStuffer(),
            ObservabilityMiddleware(),
            PolicyMiddleware(JsonPolicyEvaluator(PERMIT_ALL)),
            recorder,
        ],
    )
    await client.generate_text("hello")
    line = out.read_text().strip().splitlines()[0]
    rec = json.loads(line)
    assert rec["retrieved_contexts"] == ["doc:1 says X", "doc:2 says Y"]
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `eval/trajectory.py`**

```python
"""Trajectory recording for eval and audit.

Reuses OTel GenAI semconv attributes already written to ctx.metadata by
ObservabilityMiddleware. The recorder writes one JSONL record per request,
covering the agent's path: input, intermediate steps (currently 1 LLM call
per request; tool calls land here in later plans), and final answer.
"""
from __future__ import annotations

import json
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
        """In-memory access for callers that don't pass `out_path`."""
        return list(self._buffer)


def _flatten(req: Request) -> str:
    parts: list[str] = []
    for m in req.messages:
        parts.append(m.content if isinstance(m.content, str) else str(m.content))
    return "\n".join(parts)
```

- [ ] **Step 4: Implement `eval/__init__.py`**

```python
from eap_core.eval.trajectory import Step, Trajectory, TrajectoryRecorder

__all__ = ["Step", "Trajectory", "TrajectoryRecorder"]
```

- [ ] **Step 5: Run, expect 4 PASS. Confirm full suite still green.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/eval/__init__.py \
        packages/eap-core/src/eap_core/eval/trajectory.py \
        packages/eap-core/tests/test_eval_trajectory.py
git commit -m "feat(eval): add Trajectory model and TrajectoryRecorder middleware"
```

---

## Task 2: Judge Protocol + FaithfulnessScorer

**Files:**
- Create: `packages/eap-core/src/eap_core/eval/faithfulness.py`
- Create: `packages/eap-core/tests/test_eval_faithfulness.py`
- Modify: `packages/eap-core/src/eap_core/eval/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/eap-core/tests/test_eval_faithfulness.py
import pytest

from eap_core.eval.faithfulness import (
    DeterministicJudge,
    FaithfulnessResult,
    FaithfulnessScorer,
    Verdict,
)
from eap_core.eval.trajectory import Trajectory


async def test_deterministic_judge_extracts_sentences_as_claims():
    judge = DeterministicJudge()
    claims = await judge.extract_claims("Paris is the capital. The Eiffel Tower is iconic.")
    assert len(claims) == 2
    assert "Paris" in claims[0]


@pytest.mark.parametrize(
    "claim, contexts, expected",
    [
        ("Paris is the capital of France", ["Paris is the capital of France."], Verdict.SUPPORTED),
        ("The moon is made of cheese", ["The moon is a rocky body."], Verdict.NOT_FOUND),
        ("Paris is the capital", ["Lyon is the capital of France"], Verdict.NOT_FOUND),
    ],
)
async def test_deterministic_judge_entailment(claim, contexts, expected):
    judge = DeterministicJudge()
    verdict = await judge.entails(claim, contexts)
    assert verdict == expected


async def test_faithfulness_score_full_support():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(
        request_id="r1",
        final_answer="Paris is the capital of France. The Eiffel Tower is in Paris.",
        retrieved_contexts=[
            "Paris is the capital of France.",
            "The Eiffel Tower is in Paris and is a famous landmark.",
        ],
    )
    result = await scorer.score(traj)
    assert isinstance(result, FaithfulnessResult)
    assert result.score == 1.0
    assert len(result.per_claim) == 2
    assert all(item.verdict == Verdict.SUPPORTED for item in result.per_claim)


async def test_faithfulness_score_partial():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(
        request_id="r2",
        final_answer="Paris is the capital. Mars has two moons.",
        retrieved_contexts=["Paris is the capital of France."],
    )
    result = await scorer.score(traj)
    assert 0.0 < result.score < 1.0


async def test_faithfulness_score_zero_when_empty_answer():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(request_id="r3", final_answer="", retrieved_contexts=["x"])
    result = await scorer.score(traj)
    assert result.score == 0.0
    assert result.per_claim == []
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `eval/faithfulness.py`**

```python
"""Faithfulness scoring.

Measures: claims-in-answer-supported-by-context / total-claims-in-answer.

Uses a `Judge` Protocol so the scoring algorithm is independent of the
backing model. We ship two implementations:
- `DeterministicJudge`: substring/word-overlap based; reproducible for tests.
- `LLMJudge`: wraps an EnterpriseLLM (eval middlewares stripped to avoid recursion).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from eap_core.eval.trajectory import Trajectory


class Verdict(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    NOT_FOUND = "not_found"


class Judge(Protocol):
    async def extract_claims(self, answer: str) -> list[str]: ...
    async def entails(self, claim: str, contexts: list[str]) -> Verdict: ...


@dataclass
class ClaimResult:
    claim: str
    verdict: Verdict


class FaithfulnessResult(BaseModel):
    request_id: str = ""
    score: float
    per_claim: list[ClaimResult] = Field(default_factory=list)
    notes: str = ""

    model_config = {"arbitrary_types_allowed": True}


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\b[\w']+\b")


def _content_words(text: str) -> set[str]:
    """Lowercased word set excluding very common stopwords."""
    stop = {"a", "an", "the", "is", "are", "was", "were", "be", "of", "in", "on",
            "and", "or", "but", "to", "for", "from", "by", "at", "with", "as", "it",
            "this", "that", "these", "those", "has", "have", "had"}
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in stop}


class DeterministicJudge:
    """Reproducible judge for tests.

    - Claim extraction: split on sentence boundaries.
    - Entailment: SUPPORTED iff the claim's content words are a subset of
      the union of context content words; NOT_FOUND otherwise.
    """

    name = "deterministic"

    async def extract_claims(self, answer: str) -> list[str]:
        if not answer.strip():
            return []
        return [s.strip() for s in _SENTENCE_SPLIT.split(answer.strip()) if s.strip()]

    async def entails(self, claim: str, contexts: list[str]) -> Verdict:
        claim_words = _content_words(claim)
        if not claim_words:
            return Verdict.NOT_FOUND
        ctx_words: set[str] = set()
        for c in contexts:
            ctx_words |= _content_words(c)
        if claim_words <= ctx_words:
            return Verdict.SUPPORTED
        return Verdict.NOT_FOUND


class LLMJudge:
    """LLM-backed judge.

    Wraps an EnterpriseLLM. Caller is responsible for passing a client
    configured WITHOUT eval middlewares to avoid recursion.
    """

    name = "llm"

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]
        self._client = client

    async def extract_claims(self, answer: str) -> list[str]:
        prompt = (
            "Break the following answer into atomic factual claims. "
            "Return one claim per line, no numbering, no extra prose.\n\n"
            f"ANSWER:\n{answer}"
        )
        resp = await self._client.generate_text(prompt)
        return [line.strip() for line in resp.text.splitlines() if line.strip()]

    async def entails(self, claim: str, contexts: list[str]) -> Verdict:
        joined = "\n---\n".join(contexts) if contexts else "(no context)"
        prompt = (
            "Given the CONTEXT below, decide whether the CLAIM is supported. "
            "Answer with exactly one word: SUPPORTED, CONTRADICTED, or NOT_FOUND.\n\n"
            f"CONTEXT:\n{joined}\n\nCLAIM: {claim}\n\nVerdict:"
        )
        resp = await self._client.generate_text(prompt)
        word = resp.text.strip().split()[0].upper() if resp.text.strip() else "NOT_FOUND"
        try:
            return Verdict(word.lower())
        except ValueError:
            return Verdict.NOT_FOUND


class FaithfulnessScorer:
    name = "faithfulness"

    def __init__(self, judge: Judge) -> None:
        self._judge = judge

    async def score(self, traj: Trajectory) -> FaithfulnessResult:
        claims = await self._judge.extract_claims(traj.final_answer)
        if not claims:
            return FaithfulnessResult(request_id=traj.request_id, score=0.0)
        per_claim: list[ClaimResult] = []
        supported = 0
        for claim in claims:
            verdict = await self._judge.entails(claim, traj.retrieved_contexts)
            per_claim.append(ClaimResult(claim=claim, verdict=verdict))
            if verdict == Verdict.SUPPORTED:
                supported += 1
        return FaithfulnessResult(
            request_id=traj.request_id,
            score=supported / len(claims),
            per_claim=per_claim,
        )
```

- [ ] **Step 4: Update `eval/__init__.py`**

```python
from eap_core.eval.faithfulness import (
    ClaimResult,
    DeterministicJudge,
    FaithfulnessResult,
    FaithfulnessScorer,
    Judge,
    LLMJudge,
    Verdict,
)
from eap_core.eval.trajectory import Step, Trajectory, TrajectoryRecorder

__all__ = [
    "ClaimResult",
    "DeterministicJudge",
    "FaithfulnessResult",
    "FaithfulnessScorer",
    "Judge",
    "LLMJudge",
    "Step",
    "Trajectory",
    "TrajectoryRecorder",
    "Verdict",
]
```

- [ ] **Step 5: Run, expect 7 PASS (1 + 3 parametrized + 3 scorer tests). Confirm full suite still green.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/eval/faithfulness.py \
        packages/eap-core/src/eap_core/eval/__init__.py \
        packages/eap-core/tests/test_eval_faithfulness.py
git commit -m "feat(eval): add Judge protocol with DeterministicJudge/LLMJudge and FaithfulnessScorer"
```

---

## Task 3: Eval runner

**Files:**
- Create: `packages/eap-core/src/eap_core/eval/runner.py`
- Create: `packages/eap-core/tests/test_eval_runner.py`
- Modify: `packages/eap-core/src/eap_core/eval/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/eap-core/tests/test_eval_runner.py
import json

import pytest

from eap_core.eval.faithfulness import DeterministicJudge, FaithfulnessScorer
from eap_core.eval.runner import EvalCase, EvalRunner, EvalReport
from eap_core.eval.trajectory import Trajectory


async def _agent_returns_full_support(case: EvalCase) -> Trajectory:
    """Agent stub that always returns an answer fully supported by expected_contexts."""
    return Trajectory(
        request_id=case.id,
        final_answer=" ".join(case.expected_contexts),
        retrieved_contexts=case.expected_contexts,
    )


async def _agent_returns_unsupported(case: EvalCase) -> Trajectory:
    return Trajectory(
        request_id=case.id,
        final_answer="Mars has unicorns.",
        retrieved_contexts=case.expected_contexts,
    )


async def test_runner_scores_each_case_and_aggregates():
    cases = [
        EvalCase(id="c1", input="q1", expected_contexts=["Paris is the capital of France."]),
        EvalCase(id="c2", input="q2", expected_contexts=["The Eiffel Tower is in Paris."]),
    ]
    runner = EvalRunner(
        agent=_agent_returns_full_support,
        scorers=[FaithfulnessScorer(judge=DeterministicJudge())],
    )
    report = await runner.run(cases)
    assert isinstance(report, EvalReport)
    assert len(report.cases) == 2
    assert report.cases[0].scores["faithfulness"].score == 1.0
    assert report.aggregate["faithfulness"] == 1.0


async def test_runner_marks_failures_below_threshold():
    cases = [EvalCase(id="c1", input="q", expected_contexts=["Paris is in France."])]
    runner = EvalRunner(
        agent=_agent_returns_unsupported,
        scorers=[FaithfulnessScorer(judge=DeterministicJudge())],
        threshold=0.7,
    )
    report = await runner.run(cases)
    assert report.failed_count == 1
    assert report.passed_count == 0
    assert report.aggregate["faithfulness"] < 0.7


async def test_runner_loads_dataset_from_json(tmp_path):
    dataset = tmp_path / "golden.json"
    dataset.write_text(json.dumps([
        {"id": "c1", "input": "q1", "expected_contexts": ["X"], "expected_answer_substrings": ["X"]},
    ]))
    cases = EvalRunner.load_dataset(dataset)
    assert len(cases) == 1
    assert cases[0].id == "c1"
    assert cases[0].expected_answer_substrings == ["X"]


async def test_eval_case_minimal_fields():
    c = EvalCase(id="x", input="hi", expected_contexts=[])
    assert c.expected_answer_substrings == []
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `eval/runner.py`**

```python
"""Eval runner — drives a golden dataset through an agent and scores trajectories."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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


class _ScorerProto:
    """Duck-typed: anything with `name: str` and `async score(traj) -> FaithfulnessResult`."""

    name: str

    async def score(self, traj: Trajectory) -> FaithfulnessResult: ...  # pragma: no cover


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
            results.append(CaseResult(
                case_id=case.id,
                trajectory=traj,
                scores=scores,
                passed=passed_case,
            ))
        aggregate = {name: sum(vals) / len(vals) for name, vals in totals.items()}
        return EvalReport(
            cases=results,
            aggregate=aggregate,
            threshold=self._threshold,
            passed_count=sum(1 for r in results if r.passed),
            failed_count=sum(1 for r in results if not r.passed),
        )
```

- [ ] **Step 4: Update `eval/__init__.py`** (add runner exports)

Append `EvalCase`, `EvalReport`, `EvalRunner`, `CaseResult` to the imports and `__all__`.

- [ ] **Step 5: Run, expect 4 PASS. Confirm full suite still green.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/eval/runner.py \
        packages/eap-core/src/eap_core/eval/__init__.py \
        packages/eap-core/tests/test_eval_runner.py
git commit -m "feat(eval): add EvalRunner with EvalCase/CaseResult/EvalReport"
```

---

## Task 4: Report emitters (JSON, HTML, JUnit)

**Files:**
- Create: `packages/eap-core/src/eap_core/eval/reports.py`
- Create: `packages/eap-core/tests/test_eval_reports.py`
- Modify: `packages/eap-core/src/eap_core/eval/__init__.py`

- [ ] **Step 1: Write the failing test**

```python
# packages/eap-core/tests/test_eval_reports.py
import json
import xml.etree.ElementTree as ET

from eap_core.eval.faithfulness import (
    ClaimResult,
    FaithfulnessResult,
    Verdict,
)
from eap_core.eval.reports import emit_html, emit_json, emit_junit
from eap_core.eval.runner import CaseResult, EvalReport
from eap_core.eval.trajectory import Trajectory


def _sample_report() -> EvalReport:
    traj = Trajectory(request_id="r1", final_answer="Paris is the capital.", retrieved_contexts=["Paris..."])
    score = FaithfulnessResult(
        request_id="r1",
        score=0.5,
        per_claim=[ClaimResult(claim="Paris is the capital.", verdict=Verdict.NOT_FOUND)],
    )
    return EvalReport(
        cases=[CaseResult(case_id="c1", trajectory=traj, scores={"faithfulness": score}, passed=False)],
        aggregate={"faithfulness": 0.5},
        threshold=0.7,
        passed_count=0,
        failed_count=1,
    )


def test_emit_json_round_trips_through_loads():
    report = _sample_report()
    out = emit_json(report)
    parsed = json.loads(out)
    assert parsed["aggregate"]["faithfulness"] == 0.5
    assert parsed["cases"][0]["case_id"] == "c1"


def test_emit_html_contains_score_and_threshold():
    report = _sample_report()
    out = emit_html(report)
    assert "<html" in out.lower()
    assert "0.5" in out  # the aggregate score
    assert "c1" in out
    assert "threshold" in out.lower()


def test_emit_junit_is_valid_xml_with_one_failure():
    report = _sample_report()
    out = emit_junit(report)
    root = ET.fromstring(out)
    assert root.tag == "testsuite"
    cases = root.findall("testcase")
    assert len(cases) == 1
    assert cases[0].attrib["name"] == "c1"
    assert cases[0].find("failure") is not None
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `eval/reports.py`**

```python
"""Eval report emitters — JSON, HTML, JUnit XML."""
from __future__ import annotations

import html
from xml.etree import ElementTree as ET

from eap_core.eval.runner import EvalReport


def emit_json(report: EvalReport) -> str:
    return report.model_dump_json(indent=2)


def emit_html(report: EvalReport) -> str:
    rows: list[str] = []
    for case in report.cases:
        score_cells = " ".join(
            f"<td>{html.escape(name)}: {res.score:.2f}</td>"
            for name, res in case.scores.items()
        )
        status = "PASS" if case.passed else "FAIL"
        rows.append(
            f"<tr class='{status.lower()}'><td>{html.escape(case.case_id)}</td>"
            f"{score_cells}<td>{status}</td></tr>"
        )
    aggregate_lines = "".join(
        f"<li>{html.escape(name)}: <strong>{value:.3f}</strong></li>"
        for name, value in report.aggregate.items()
    )
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>EAP-Core eval report</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; }}
  td, th {{ padding: .5rem 1rem; border-bottom: 1px solid #ddd; }}
  tr.pass {{ background: #f1faf1; }}
  tr.fail {{ background: #fbeaea; }}
</style></head>
<body>
<h1>EAP-Core eval report</h1>
<p>Threshold: <strong>{report.threshold}</strong> &mdash;
Passed: {report.passed_count} / Failed: {report.failed_count}</p>
<h2>Aggregate</h2><ul>{aggregate_lines}</ul>
<h2>Cases</h2>
<table><tr><th>Case</th><th>Scores</th><th>Status</th></tr>
{''.join(rows)}
</table>
</body></html>
"""


def emit_junit(report: EvalReport) -> str:
    suite = ET.Element(
        "testsuite",
        attrib={
            "name": "eap-core-eval",
            "tests": str(len(report.cases)),
            "failures": str(report.failed_count),
        },
    )
    for case in report.cases:
        scores_str = ", ".join(f"{n}={r.score:.3f}" for n, r in case.scores.items())
        tc = ET.SubElement(
            suite,
            "testcase",
            attrib={"name": case.case_id, "classname": "eap_core.eval"},
        )
        if not case.passed:
            failure = ET.SubElement(
                tc,
                "failure",
                attrib={"message": f"Below threshold {report.threshold}: {scores_str}"},
            )
            failure.text = scores_str
    return ET.tostring(suite, encoding="unicode")
```

- [ ] **Step 4: Update `eval/__init__.py`** to export the emitters.

- [ ] **Step 5: Run, expect 3 PASS.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/eval/reports.py \
        packages/eap-core/src/eap_core/eval/__init__.py \
        packages/eap-core/tests/test_eval_reports.py
git commit -m "feat(eval): add JSON/HTML/JUnit report emitters"
```

---

## Task 5: Ragas adapter (`[eval]` extra)

**Files:**
- Create: `packages/eap-core/src/eap_core/eval/ragas_adapter.py`
- Create: `packages/eap-core/tests/extras/test_ragas_adapter.py`

- [ ] **Step 1: Write the extras test**

```python
# packages/eap-core/tests/extras/test_ragas_adapter.py
import pytest

pytest.importorskip("ragas")
pytestmark = pytest.mark.extras

from eap_core.eval.ragas_adapter import to_ragas_dataset
from eap_core.eval.trajectory import Trajectory


def test_converts_trajectories_to_ragas_dataset_dicts():
    trajs = [
        Trajectory(
            request_id="r1",
            final_answer="Paris is the capital.",
            retrieved_contexts=["Paris is the capital of France."],
            extra={"input_text": "What is the capital of France?"},
        ),
        Trajectory(
            request_id="r2",
            final_answer="Lyon is in France.",
            retrieved_contexts=["Lyon is the third-largest city in France."],
            extra={"input_text": "Where is Lyon?"},
        ),
    ]
    rows = to_ragas_dataset(trajs)
    # Ragas expects: question, answer, contexts (list[str]), [ground_truth].
    assert len(rows) == 2
    assert rows[0]["question"] == "What is the capital of France?"
    assert rows[0]["answer"] == "Paris is the capital."
    assert rows[0]["contexts"] == ["Paris is the capital of France."]
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `eval/ragas_adapter.py`**

```python
"""Ragas dataset adapter — convert Trajectory list to Ragas-friendly rows.

Ragas's `EvaluationDataset.from_list(...)` accepts a list of dicts with
question/answer/contexts (and optional ground_truth) keys. This adapter
maps our Trajectory fields to that shape so callers can plug into the
Ragas pipeline.
"""
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
```

- [ ] **Step 4: Run extras test, expect PASS or SKIP.**

Ragas was installed by Plan 1's `--all-extras`. If the import works, the test runs and should pass. If Ragas isn't reachable in the venv (it can be heavy and sometimes fails to install cleanly), `pytest.importorskip` will skip it.

If the test fails with an unexpected Ragas API change (`EvaluationDataset.from_list` doesn't exist, etc.), the failure is in *our* import-time check (`pytest.importorskip("ragas")`) or in the assertions about output dict shape — neither depends on the Ragas API. Should be robust.

- [ ] **Step 5: Commit**

```bash
git add packages/eap-core/src/eap_core/eval/ragas_adapter.py \
        packages/eap-core/tests/extras/test_ragas_adapter.py
git commit -m "feat(eval): add Ragas dataset adapter (eval extra)"
```

---

## Done conditions for this plan

When all tasks are complete:

1. `TrajectoryRecorder` middleware drops into the chain and writes JSONL traces or buffers them in memory.
2. `FaithfulnessScorer` produces a deterministic score in tests via `DeterministicJudge`, and is ready for production with `LLMJudge`.
3. `EvalRunner` consumes a golden-set JSON file, runs cases through a user-provided agent, scores via configured scorers, and returns an `EvalReport`.
4. `emit_json` / `emit_html` / `emit_junit` produce readable reports.
5. The Ragas extra test passes (or skips cleanly).
6. Full suite green; coverage stays ≥ 90% on `eap_core`.
7. Plan 4 (CLI) wires `eap eval` as a thin shell over `EvalRunner.load_dataset` + `EvalRunner.run` + the right emitter.
