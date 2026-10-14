"""Load `.review-agent.yaml` and build the agent roster from it.

The config is versioned in the repo under review, same idea as the PRD's
`.kiro/ci-rules.yaml`: which specialists run, which model each one routes
to, and the aggregation thresholds. Changing the roster is a PR, not a
secret prompt in someone's head.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from review_agent.agents.base import ReviewAgent
from review_agent.agents.external_cli_agent import ExternalCLIAgent
from review_agent.agents.fake_agent import FakeAgent
from review_agent.agents.heuristic_agent import HeuristicAgent
from review_agent.agents.litellm_agent import LiteLLMAgent
from review_agent.agents.verifier import LiteLLMVerifier, Verifier
from review_agent.schemas import Category


class AgentSpec(BaseModel):
    id: str
    type: str  # "llm" | "cli" | "fake" | "heuristic"
    # baseline: the single-agent control (arms A/B). specialist: fanned out
    # in arms C/D. verifier: second-stage filter. Explicit, never positional.
    role: Literal["baseline", "specialist", "verifier"] = "specialist"
    category: Category = Category.OTHER
    focus: str = ""
    model: str = "gpt-4o-mini"
    api_base: str | None = None      # e.g. an OpenAI-compatible proxy: https://host/v1
    api_key_env: str | None = None   # name of the env var holding the token; never the token itself
    temperature: float | None = 0    # null = don't send (reasoning models fix their own)
    command: list[str] = Field(default_factory=list)
    max_findings: int = 10
    enabled: bool = True


class AggregationSpec(BaseModel):
    line_window: int = 3
    max_per_category: int = 10
    max_total: int = 25


class LimitsSpec(BaseModel):
    """Cost guardrails. A review that silently sends a 5,000-line PR to a
    frontier model is a bill, not a review; anything cut is reported."""
    max_files: int = 40
    max_hunks: int = 150
    max_hunk_lines: int = 400       # bigger hunks are generated or vendored code
    max_prompt_chars: int = 120_000  # ~30k tokens of diff; hunks past this are dropped, in order


# Files whose diffs are never worth a model call.
DEFAULT_EXCLUDE = [
    "*/vendor/*", "*/node_modules/*", "*/dist/*", "*/build/*", "*/.venv/*",
    "*.min.js", "*.min.css", "*.lock", "package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock",
    "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*.generated.*", "*.snap", "*.svg", "*.map",
]


class Config(BaseModel):
    version: str = "1"
    agents: list[AgentSpec] = Field(default_factory=list)
    aggregation: AggregationSpec = Field(default_factory=AggregationSpec)
    per_agent_timeout_s: float = 180.0
    # flag: a verifier verdict downgrades the finding to INFO and annotates it;
    # drop: it is removed from the posted set (kept in the report for audit).
    # flag is the default because the verifier has killed a real finding on
    # real code; drop is what the benchmark measures.
    verifier_mode: Literal["flag", "drop"] = "flag"
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    rules: dict[str, Any] = Field(default_factory=dict)


GENERALIST_FOCUS = (
    "any real defect: security (auth/permission bypass, injection, path traversal, hardcoded secrets, "
    "unsafe deserialization), logic (races/TOCTOU, off-by-one, None handling, stale closures, swallowed "
    "exceptions), performance (N+1 queries, O(n*m) loops, redundant work per iteration)"
)

# Default shape: one generalist finder + one verifier. Category specialists
# are opt-in. On the benchmark (bench-results/) a single generalist call of
# the same model matched three specialists on recall with fewer duplicates,
# no clean-case false positives and under half the agent time; the
# specialists' only reproducible extra output was a performance nit on a
# harmless refactor. The verifier dropped that nit and lost 1 truth in 110.
DEFAULT_CONFIG = Config(
    agents=[
        AgentSpec(id="generalist", type="llm", role="baseline", category=Category.OTHER,
                  focus=GENERALIST_FOCUS, max_findings=10),
        AgentSpec(id="verifier", type="llm", role="verifier"),
    ]
)


def load_config(path: Path | None) -> Config:
    if path is None or not path.exists():
        return DEFAULT_CONFIG
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def _resolve_api_key(spec: AgentSpec) -> str | None:
    if not spec.api_key_env:
        return None
    api_key = os.environ.get(spec.api_key_env)
    if not api_key:
        # Fail here, before any diff is read or token spent.
        raise ValueError(f"agent '{spec.id}': env var {spec.api_key_env} is not set")
    return api_key


def build_verifiers(config: Config) -> list[Verifier]:
    """Second-stage filters: specs with role=verifier. Only LLM-backed for now."""
    verifiers: list[Verifier] = []
    for spec in config.agents:
        if not spec.enabled or spec.role != "verifier":
            continue
        if spec.type != "llm":
            raise ValueError(f"verifier '{spec.id}': only type 'llm' is supported, got '{spec.type}'")
        verifiers.append(LiteLLMVerifier(verifier_id=spec.id, model=spec.model, api_base=spec.api_base,
                                         api_key=_resolve_api_key(spec), temperature=spec.temperature))
    return verifiers


def build_agents(config: Config, repo_root: Path | None = None) -> list[ReviewAgent]:
    """First-stage reviewers. Verifier specs are built by build_verifiers, not here."""
    agents: list[ReviewAgent] = []
    for spec in config.agents:
        if not spec.enabled or spec.role == "verifier":
            continue
        if spec.type == "llm":
            agents.append(LiteLLMAgent(agent_id=spec.id, focus=spec.focus, category=spec.category,
                                       model=spec.model, max_findings=spec.max_findings,
                                       api_base=spec.api_base, api_key=_resolve_api_key(spec),
                                       temperature=spec.temperature))
        elif spec.type == "cli":
            agents.append(ExternalCLIAgent(agent_id=spec.id, command=spec.command, category=spec.category,
                                           cwd=str(repo_root) if repo_root else None))
        elif spec.type == "fake":
            agents.append(FakeAgent(agent_id=spec.id, category=spec.category))
        elif spec.type == "heuristic":
            agents.append(HeuristicAgent(agent_id=spec.id))
        else:
            raise ValueError(f"unknown agent type '{spec.type}' for agent '{spec.id}'")
        agents[-1].role = spec.role
    return agents
