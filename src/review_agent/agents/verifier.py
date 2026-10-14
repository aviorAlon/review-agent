"""Second-stage verifier: attacks precision instead of recall.

Specialists fan out to *find* things. The verifier sees the whole candidate
list next to the diff and removes what should not reach the author: claims
the shown code contradicts, lines that don't contain what the finding says,
speculation, nits, and duplicates the rule-based merge could not fold.

Two rules that matter more than the prompt:

- FAIL-OPEN. A finding is dropped only when a verifier affirmatively returns
  `drop` for its index. A verifier error, a malformed reply, or an omitted
  index keeps the finding. A filter that can delete findings by crashing is
  worse than no filter.
- NOTHING IS LOST. Dropped findings are returned separately with the
  verifier's reason, so the benchmark can score what was killed (dropped
  true positives are the verifier's own false positives) and a human can
  audit the decision on a real MR.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from review_agent.agents.litellm_agent import _extract_json_array, _render_user_prompt, _usage_of
from review_agent.schemas import ContextBundle, Finding


@dataclass
class Verdict:
    index: int
    keep: bool
    reason: str = ""


@dataclass
class VerifierResult:
    verifier_id: str
    verdicts: list[Verdict] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Verifier(ABC):
    verifier_id: str
    role: str = "verifier"
    last_usage: tuple[int, int] | None = None

    async def verify(self, findings: list[Finding], context: ContextBundle) -> VerifierResult:
        start = time.monotonic()
        self.last_usage = None
        try:
            verdicts = await self._run(findings, context)
            pt, ct = self.last_usage or (0, 0)
            return VerifierResult(self.verifier_id, verdicts, elapsed_ms=int((time.monotonic() - start) * 1000),
                                  prompt_tokens=pt, completion_tokens=ct)
        except Exception as exc:  # noqa: BLE001 - verifier failure must never delete findings
            pt, ct = self.last_usage or (0, 0)
            return VerifierResult(self.verifier_id, [], error=str(exc), elapsed_ms=int((time.monotonic() - start) * 1000),
                                  prompt_tokens=pt, completion_tokens=ct)

    @abstractmethod
    async def _run(self, findings: list[Finding], context: ContextBundle) -> list[Verdict]:
        raise NotImplementedError


def apply_verdicts(findings: list[Finding], results: list[VerifierResult]) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (kept, dropped). Only an explicit `drop` removes anything."""
    drop_reason: dict[int, str] = {}
    for r in results:
        if r.error:
            continue
        for v in r.verdicts:
            if not v.keep and 0 <= v.index < len(findings):
                drop_reason.setdefault(v.index, f"{r.verifier_id}: {v.reason}")
    kept: list[Finding] = []
    dropped: list[Finding] = []
    for i, f in enumerate(findings):
        if i in drop_reason:
            dropped.append(f.model_copy(update={"dropped_reason": drop_reason[i]}))
        else:
            kept.append(f)
    return kept, dropped


# ------------------------------------------------------------------ backends

class FakeVerifier(Verifier):
    """Deterministic: drops every finding the predicate matches. For tests."""

    def __init__(self, drop_if: Callable[[Finding], bool], verifier_id: str = "fake-verifier"):
        self.verifier_id = verifier_id
        self.drop_if = drop_if

    async def _run(self, findings: list[Finding], context: ContextBundle) -> list[Verdict]:
        return [Verdict(i, keep=not self.drop_if(f), reason="predicate") for i, f in enumerate(findings)]


VERIFIER_SYSTEM_PROMPT = """You are a skeptical second-pass code reviewer. You receive a code change and a
numbered list of candidate findings written by other reviewers. Your only job
is to decide which candidates must NOT be shown to the author.

Drop a candidate when:
- the cited line does not contain what the finding describes, or the code
  shown contradicts the claim;
- it is speculative (no evidence in the change), a style nit, or not actionable;
- it duplicates another candidate that already covers the same defect on the
  same lines -- keep the clearer one, drop the rest.
Keep a candidate when the defect is real and visible in the change, even if minor.
When unsure, keep.

Respond with ONLY a JSON array, no prose, no markdown fences, exactly one
element per candidate:
[{"index": int, "verdict": "keep"|"drop", "reason": str}]
"""


def _render_candidates(findings: list[Finding]) -> str:
    lines = ["# Candidate findings"]
    for i, f in enumerate(findings):
        lines.append(f"[{i}] {f.file}:{f.line} [{f.category.value}/{f.severity.value}] {f.message}")
        if f.reasoning:
            lines.append(f"     reasoning: {f.reasoning}")
    return "\n".join(lines)


class LiteLLMVerifier(Verifier):
    def __init__(
        self,
        verifier_id: str,
        model: str = "gpt-4o-mini",
        api_base: str | None = None,
        api_key: str | None = None,
        temperature: float | None = 0,
    ):
        self.verifier_id = verifier_id
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.temperature = temperature

    async def _run(self, findings: list[Finding], context: ContextBundle) -> list[Verdict]:
        import litellm

        candidates = [f for f in findings if not f.unstructured]
        if not candidates:
            return []

        user_prompt = _render_user_prompt(context) + "\n\n" + _render_candidates(findings)
        extra: dict = {"drop_params": True}
        if self.temperature is not None:
            extra["temperature"] = self.temperature
        if self.api_base:
            extra["api_base"] = self.api_base
        if self.api_key:
            extra["api_key"] = self.api_key
        response = await litellm.acompletion(
            model=self.model,
            messages=[
                {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            **extra,
        )
        self.last_usage = _usage_of(response)
        content = response.choices[0].message.content or ""
        raw = _extract_json_array(content)
        items = json.loads(raw) if raw is not None else []

        verdicts: list[Verdict] = []
        for item in items:
            try:
                idx = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            keep = str(item.get("verdict", "keep")).strip().lower() != "drop"
            verdicts.append(Verdict(idx, keep=keep, reason=str(item.get("reason", ""))[:300]))
        return verdicts
