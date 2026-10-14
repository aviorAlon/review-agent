"""The one contract every agent backend must satisfy.

This is the whole point of "pluggable agents": the orchestrator only ever
talks to this interface. A backend can be a single LLM prompt call, a full
tool-using agent loop, or a subprocess wrapping an entirely separate coding
agent CLI (Claude Code, Aider, a Kiro headless agent, whatever) -- as long
as it returns an AgentResult, the orchestrator can't tell the difference.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from review_agent.schemas import AgentResult, ContextBundle


class ReviewAgent(ABC):
    agent_id: str
    # "baseline" = the single-agent control the ablation compares against,
    # "specialist" = one of the fanned-out reviewers, "verifier" = a second
    # stage that filters findings. Set from config; defaults to specialist.
    role: str = "specialist"

    @abstractmethod
    async def review(self, context: ContextBundle) -> AgentResult:
        """Run this agent against the given context and return findings.

        Implementations should catch their own exceptions and return an
        AgentResult with `error` set rather than raising -- one backend
        failing must never take down the whole fan-out.
        """
        raise NotImplementedError


class TimedAgent(ReviewAgent):
    """Convenience base class that fills in elapsed_ms, token usage and error handling.

    A backend that knows its token usage sets `self.last_usage = (prompt, completion)`
    inside `_run`; it is reset before every call so nothing stale leaks across runs.
    """

    last_usage: tuple[int, int] | None = None

    async def review(self, context: ContextBundle) -> AgentResult:
        start = time.monotonic()
        self.last_usage = None
        try:
            findings = await self._run(context)
            elapsed = int((time.monotonic() - start) * 1000)
            pt, ct = self.last_usage or (0, 0)
            return AgentResult(agent_id=self.agent_id, findings=findings, elapsed_ms=elapsed,
                               prompt_tokens=pt, completion_tokens=ct)
        except Exception as exc:  # noqa: BLE001 - a backend failure is data, not a crash
            elapsed = int((time.monotonic() - start) * 1000)
            pt, ct = self.last_usage or (0, 0)
            return AgentResult(agent_id=self.agent_id, findings=[], error=str(exc), elapsed_ms=elapsed,
                               prompt_tokens=pt, completion_tokens=ct)

    @abstractmethod
    async def _run(self, context: ContextBundle) -> list:
        raise NotImplementedError
