"""Fan-out: run every agent in parallel against one shared ContextBundle.

Design rules that matter for the ablation study:
- Context is built ONCE and shared, so agents never see different inputs.
- Every agent has its own timeout; a slow/broken backend yields an
  AgentResult with `error` set, it never cancels the others. Partial results
  are results. A timed-out agent silently vanishing would corrupt the sample.
"""
from __future__ import annotations

import asyncio

from review_agent.agents.base import ReviewAgent
from review_agent.schemas import AgentResult, ContextBundle


class Orchestrator:
    def __init__(self, agents: list[ReviewAgent], per_agent_timeout_s: float = 180.0):
        if not agents:
            raise ValueError("Orchestrator needs at least one agent")
        self.agents = agents
        self.per_agent_timeout_s = per_agent_timeout_s

    async def run(self, context: ContextBundle) -> list[AgentResult]:
        tasks = [self._run_one(agent, context) for agent in self.agents]
        return await asyncio.gather(*tasks)

    async def _run_one(self, agent: ReviewAgent, context: ContextBundle) -> AgentResult:
        try:
            return await asyncio.wait_for(agent.review(context), timeout=self.per_agent_timeout_s)
        except asyncio.TimeoutError:
            return AgentResult(
                agent_id=agent.agent_id,
                findings=[],
                error=f"timed out after {self.per_agent_timeout_s}s",
                elapsed_ms=int(self.per_agent_timeout_s * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - defensive: a backend that forgot to catch its own errors
            return AgentResult(agent_id=agent.agent_id, findings=[], error=str(exc))
