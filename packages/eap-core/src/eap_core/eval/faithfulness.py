"""Faithfulness scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from eap_core.eval.trajectory import Trajectory


class Verdict(StrEnum):
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
    stop = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "of",
        "in",
        "on",
        "and",
        "or",
        "but",
        "to",
        "for",
        "from",
        "by",
        "at",
        "with",
        "as",
        "it",
        "this",
        "that",
        "these",
        "those",
        "has",
        "have",
        "had",
    }
    return {w.lower() for w in _WORD.findall(text) if w.lower() not in stop}


class DeterministicJudge:
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
    """Production judge backed by an EnterpriseLLM client.

    Tested against a real LLM in eval pipelines, not in unit tests — the
    DeterministicJudge covers the FaithfulnessScorer's algorithmic path.
    """

    name = "llm"

    def __init__(self, client) -> None:  # type: ignore[no-untyped-def]  # pragma: no cover
        self._client = client

    async def extract_claims(self, answer: str) -> list[str]:  # pragma: no cover
        prompt = (
            "Break the following answer into atomic factual claims. "
            "Return one claim per line, no numbering, no extra prose.\n\n"
            f"ANSWER:\n{answer}"
        )
        resp = await self._client.generate_text(prompt)
        return [line.strip() for line in resp.text.splitlines() if line.strip()]

    async def entails(self, claim: str, contexts: list[str]) -> Verdict:  # pragma: no cover
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
