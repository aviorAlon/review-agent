"""Core data contracts shared by every module.

These are the only types that cross module boundaries. Any new agent
backend, context source, or VCS provider must speak these shapes and
nothing else -- that is what keeps the system pluggable.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Category(str, Enum):
    SECURITY = "security"
    LOGIC = "logic"
    PERFORMANCE = "performance"
    TEST_COVERAGE = "test_coverage"
    STYLE = "style"
    OTHER = "other"


class Hunk(BaseModel):
    """One contiguous block of changed lines in one file."""

    file: str
    start_line: int
    end_line: int
    added_lines: list[str] = Field(default_factory=list)
    added_line_numbers: list[int] = Field(default_factory=list)  # target-side line of each added line
    removed_lines: list[str] = Field(default_factory=list)
    # For each removed line: the target-side line number it followed (0 = before
    # the first line). Position is meaning -- rendering removed lines at the end
    # made `if user is None:` look like dead code below `return user.email`.
    removed_anchors: list[int] = Field(default_factory=list)
    # Unchanged lines inside the hunk. Without them a reviewer cannot see that
    # `s += prices[i]` sits under a changed `range(len(prices) + 1)`.
    context_lines: list[str] = Field(default_factory=list)
    context_line_numbers: list[int] = Field(default_factory=list)


class SymbolRef(BaseModel):
    """A function/class touched by a hunk, plus who calls or is called by it."""

    name: str
    file: str
    line: int
    signature: str | None = None
    callers: list[str] = Field(default_factory=list)          # "file:line" locations
    callees: list[str] = Field(default_factory=list)          # names called from inside the symbol
    # The parts that let a reviewer judge a change by how it is used, not by
    # how it looks: numbered code around each call site, and the bodies of
    # project-defined functions the changed code calls.
    caller_snippets: list[str] = Field(default_factory=list)
    callee_defs: list[str] = Field(default_factory=list)


class ContextBundle(BaseModel):
    """Everything an agent gets besides the raw diff: structural context."""

    hunks: list[Hunk]
    symbols: list[SymbolRef] = Field(default_factory=list)
    repo_rules: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    """One reviewable issue, in the shape every backend must emit."""

    file: str
    line: int
    severity: Severity
    category: Category
    message: str
    reasoning: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    source_agent: str = "unknown"
    merged_from: list[str] = Field(default_factory=list)  # other source_agent ids folded into this one
    # True when a backend emitted free text we could not place on a line. Kept
    # for audit, excluded from scoring and from inline posting.
    unstructured: bool = False
    # Set when a verifier judged this finding should not reach the author. In
    # `drop` mode it then lives in report.dropped; in `flag` mode it stays in
    # report.findings downgraded to INFO with `flagged=True`.
    dropped_reason: str = ""
    flagged: bool = False


def parse_category(raw: object, default: Category) -> Category:
    """Model-reported category, falling back to the agent's default when absent/invalid."""
    try:
        return Category(str(raw))
    except ValueError:
        return default


class AgentResult(BaseModel):
    """What one agent backend returns for one review run."""

    agent_id: str
    findings: list[Finding]
    error: str | None = None
    elapsed_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
