import asyncio

import pytest

from review_agent.agents.base import ReviewAgent, TimedAgent
from review_agent.agents.fake_agent import FakeAgent
from review_agent.orchestrator.orchestrator import Orchestrator
from review_agent.pipeline import run_review
from review_agent.schemas import AgentResult, Category, ContextBundle, Finding, Severity


class SlowAgent(ReviewAgent):
    agent_id = "slow"

    async def review(self, context):
        await asyncio.sleep(10)
        return AgentResult(agent_id=self.agent_id, findings=[])


class ExplodingAgent(TimedAgent):
    agent_id = "boom"

    async def _run(self, context):
        raise RuntimeError("backend blew up")


class OneFinding(TimedAgent):
    def __init__(self, agent_id, line):
        self.agent_id = agent_id
        self.line = line

    async def _run(self, context):
        return [Finding(file="a.py", line=self.line, severity=Severity.WARNING, category=Category.LOGIC,
                        message="x", source_agent=self.agent_id)]


@pytest.mark.asyncio
async def test_timeout_isolated_to_one_agent():
    orch = Orchestrator([SlowAgent(), OneFinding("fast", 1)], per_agent_timeout_s=0.05)
    results = await orch.run(ContextBundle(hunks=[]))
    by_id = {r.agent_id: r for r in results}
    assert "timed out" in by_id["slow"].error
    assert by_id["fast"].error is None and len(by_id["fast"].findings) == 1


@pytest.mark.asyncio
async def test_exception_becomes_error_result():
    results = await Orchestrator([ExplodingAgent()]).run(ContextBundle(hunks=[]))
    assert results[0].error == "backend blew up"
    assert results[0].findings == []


def test_orchestrator_requires_agents():
    with pytest.raises(ValueError):
        Orchestrator([])


@pytest.mark.asyncio
async def test_pipeline_with_and_without_context(sample_diff, fake_repo):
    agents = [FakeAgent("fake-a"), FakeAgent("fake-b", Category.SECURITY)]

    with_ctx = await run_review(sample_diff, fake_repo, agents, use_context_engine=True)
    assert len(with_ctx.context.symbols) == 1
    # check_permission has a caller, so FakeAgent's no-caller heuristic stays quiet
    assert with_ctx.findings == []

    without_ctx = await run_review(sample_diff, fake_repo, agents, use_context_engine=False)
    assert without_ctx.context.symbols == []
    assert without_ctx.findings == []


@pytest.mark.asyncio
async def test_pipeline_merges_agreeing_agents(sample_diff, fake_repo):
    agents = [OneFinding("a", 3), OneFinding("b", 4)]
    report = await run_review(sample_diff, fake_repo, agents)
    assert len(report.findings) == 1
    assert sorted([report.findings[0].source_agent, *report.findings[0].merged_from]) == ["a", "b"]
