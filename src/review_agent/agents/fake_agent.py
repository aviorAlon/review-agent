"""Deterministic test/demo backend -- no network, no API key required.

Used for unit tests and for a zero-setup `review-agent demo` run so the
whole pipeline is runnable before anyone wires up a real LLM key.
"""
from __future__ import annotations

from review_agent.agents.base import TimedAgent
from review_agent.schemas import Category, ContextBundle, Finding, Severity


class FakeAgent(TimedAgent):
    """Flags any hunk that touches a symbol with no detected callers as
    a trivial made-up heuristic, purely to exercise the pipeline end to end.
    """

    def __init__(self, agent_id: str = "fake-agent", category: Category = Category.LOGIC, role: str = "specialist"):
        self.agent_id = agent_id
        self.category = category
        self.role = role

    async def _run(self, context: ContextBundle) -> list[Finding]:
        findings: list[Finding] = []
        for symbol in context.symbols:
            if not symbol.callers:
                findings.append(
                    Finding(
                        file=symbol.file,
                        line=symbol.line,
                        severity=Severity.INFO,
                        category=self.category,
                        message=f"'{symbol.name}' has no detected callers in this repo.",
                        reasoning="Heuristic demo finding from FakeAgent, not a real analysis.",
                        confidence=0.4,
                        source_agent=self.agent_id,
                    )
                )
        return findings
