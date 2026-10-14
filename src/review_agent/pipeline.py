"""One function that runs the whole thing:
diff -> scope -> context -> fan-out -> aggregate -> (verify) -> findings.

Kept separate from the CLI so tests and future CI adapters (GitHub Action,
webhook server, Jenkins shared lib) all call the same entry point. The
`use_context_engine`, `verifiers` and `verifier_mode` knobs exist so the
validation study can switch each stage off without touching anything else.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from review_agent.agents.base import ReviewAgent
from review_agent.agents.verifier import Verifier, VerifierResult, apply_verdicts
from review_agent.context.engine import ContextEngine
from review_agent.diff import parse_diff
from review_agent.orchestrator.dedup import aggregate
from review_agent.orchestrator.orchestrator import Orchestrator
from review_agent.schemas import AgentResult, ContextBundle, Finding, Hunk, Severity


@dataclass
class ReviewReport:
    context: ContextBundle
    agent_results: list[AgentResult]
    findings: list[Finding]
    dropped: list[Finding] = field(default_factory=list)   # verifier_mode=drop
    flagged: list[Finding] = field(default_factory=list)   # verifier_mode=flag (also present in findings)
    verifier_results: list[VerifierResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)         # what was cut by exclude/limits, for the summary
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    unsupported_files: list[str] = field(default_factory=list)  # languages the context engine could not index


def _excluded(path: str, patterns: list[str]) -> bool:
    # fnmatch's `*` crosses `/`, so "*/vendor/*" matches any depth; also try
    # the bare basename so "*.lock" catches "sub/dir/x.lock" on every platform.
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, p) or fnmatch(name, p) or fnmatch("/" + path, p) for p in patterns)


def scope_hunks(hunks: list[Hunk], exclude: list[str] | None = None, limits=None) -> tuple[list[Hunk], list[str]]:
    """Apply exclude globs and cost limits. Returns (kept hunks, human-readable notes)."""
    notes: list[str] = []
    excluded_files: list[str] = []
    kept: list[Hunk] = []
    for h in hunks:
        if exclude and _excluded(h.file, exclude):
            if h.file not in excluded_files:
                excluded_files.append(h.file)
            continue
        if limits is not None:
            size = len(h.added_lines) + len(h.removed_lines) + len(h.context_lines)
            if size > limits.max_hunk_lines:
                notes.append(f"skipped {h.file}:{h.start_line} ({size} lines > max_hunk_lines {limits.max_hunk_lines})")
                continue
        kept.append(h)
    if excluded_files:
        notes.append(f"excluded {len(excluded_files)} file(s) by pattern: {', '.join(excluded_files[:8])}" + (" ..." if len(excluded_files) > 8 else ""))

    if limits is None:
        return kept, notes

    files: list[str] = []
    out: list[Hunk] = []
    chars = 0
    cut_files = cut_hunks = cut_budget = 0
    for h in kept:
        if h.file not in files:
            if len(files) >= limits.max_files:
                cut_files += 1
                continue
            files.append(h.file)
        if len(out) >= limits.max_hunks:
            cut_hunks += 1
            continue
        size_chars = sum(len(line) + 8 for line in h.added_lines + h.removed_lines + h.context_lines)
        if chars + size_chars > limits.max_prompt_chars:
            cut_budget += 1
            continue
        chars += size_chars
        out.append(h)
    if cut_files:
        notes.append(f"skipped {cut_files} hunk(s) beyond max_files {limits.max_files}")
    if cut_hunks:
        notes.append(f"skipped {cut_hunks} hunk(s) beyond max_hunks {limits.max_hunks}")
    if cut_budget:
        notes.append(f"skipped {cut_budget} hunk(s) beyond max_prompt_chars {limits.max_prompt_chars}")
    return out, notes


async def run_review(
    diff_text: str,
    repo_root: Path,
    agents: list[ReviewAgent],
    repo_rules: dict | None = None,
    use_context_engine: bool = True,
    per_agent_timeout_s: float = 180.0,
    line_window: int = 3,
    max_per_category: int = 10,
    max_total: int = 25,
    verifiers: list[Verifier] | None = None,
    verifier_mode: str = "drop",
    exclude: list[str] | None = None,
    limits=None,
) -> ReviewReport:
    hunks, notes = scope_hunks(parse_diff(diff_text), exclude, limits)

    unsupported: list[str] = []
    if use_context_engine:
        engine = ContextEngine(repo_root)
        context = engine.build(hunks, repo_rules=repo_rules)
        unsupported = engine.unsupported_files
        if unsupported:
            notes.append(f"diff-only context for {len(unsupported)} file(s) in languages without an index: "
                         + ", ".join(unsupported[:6]) + (" ..." if len(unsupported) > 6 else ""))
    else:
        context = ContextBundle(hunks=hunks, repo_rules=repo_rules or {})

    results = await Orchestrator(agents, per_agent_timeout_s=per_agent_timeout_s).run(context)
    findings = aggregate(results, line_window=line_window, max_per_category=max_per_category, max_total=max_total)

    dropped: list[Finding] = []
    flagged: list[Finding] = []
    verifier_results: list[VerifierResult] = []
    if verifiers and findings:
        # Every verifier judges the same aggregated list, so indices agree.
        for v in verifiers:
            verifier_results.append(await v.verify(findings, context))
        findings, dropped = apply_verdicts(findings, verifier_results)
        if verifier_mode == "flag":
            flagged = [f.model_copy(update={"severity": Severity.INFO, "flagged": True}) for f in dropped]
            findings = findings + flagged
            dropped = []

    return ReviewReport(context=context, agent_results=results, findings=findings, dropped=dropped,
                        flagged=flagged, verifier_results=verifier_results, notes=notes,
                        unsupported_files=unsupported)
