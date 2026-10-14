"""PR-Agent as a baseline backend. EXPERIMENTAL and unverified: see below.

PR-Agent (qodo-ai/pr-agent, Apache-2.0) is treated as *one agent among
several*, not as the framework we live inside. That gives us a free,
like-for-like baseline: ablation arm A is literally "only this backend,
no context engine".

PR-Agent's `/review` needs a real PR URL plus git-provider credentials and
emits markdown, not our JSON schema. So this backend only works in PR mode
(not local-diff mode) and leans on ExternalCLIAgent's raw-text fallback for
parsing. Tightening that into structured findings is a follow-up; for the
ablation study the raw markdown is still blind-labelable.
"""
from __future__ import annotations

import shutil

from review_agent.agents.external_cli_agent import ExternalCLIAgent
from review_agent.schemas import AgentResult, Category, ContextBundle


class PRAgentBackend(ExternalCLIAgent):
    def __init__(self, pr_url: str | None, agent_id: str = "pr-agent-baseline", cwd: str | None = None):
        self.pr_url = pr_url
        command = ["pr-agent", "--pr_url", pr_url or "", "review"]
        super().__init__(agent_id=agent_id, command=command, category=Category.OTHER, cwd=cwd)

    async def review(self, context: ContextBundle) -> AgentResult:
        if not self.pr_url:
            return AgentResult(agent_id=self.agent_id, findings=[], error="PRAgentBackend needs --pr-url (PR mode only)")
        if shutil.which("pr-agent") is None:
            return AgentResult(agent_id=self.agent_id, findings=[], error="pr-agent CLI not installed (pip install pr-agent)")
        return await super().review(context)
