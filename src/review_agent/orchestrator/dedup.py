"""Rule-based aggregation: merge, rank, cap.

Deliberately deterministic for the validation phase. If an LLM did the
merging, its own mistakes would be entangled with the generation agents'
quality and the ablation couldn't attribute anything. Rules here:

  1. Two findings are duplicates if same file, same category, and lines
     within `line_window` of each other. Keep the higher-confidence one and
     record the loser's source_agent in `merged_from` so merges stay
     auditable (we can later count mis-merges instead of guessing).
  2. Same-file, different-line, same-root-cause duplicates are NOT handled.
     Rules can't see root cause. We prefer showing a duplicate to silently
     collapsing two real issues into one -- the LLM-arbiter upgrade is a
     separate, separately-validated step.
  3. Rank by severity, then confidence. Cap per category with
     `max_per_category`, and overall with `max_total`.
"""
from __future__ import annotations

from collections import defaultdict

from review_agent.schemas import AgentResult, Finding, Severity

_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _is_duplicate(a: Finding, b: Finding, line_window: int) -> bool:
    if a.unstructured or b.unstructured:
        return False
    return a.file == b.file and a.category == b.category and abs(a.line - b.line) <= line_window


def merge_findings(findings: list[Finding], line_window: int = 3) -> list[Finding]:
    # Process highest-confidence first so the survivor of each cluster is the best one.
    ordered = sorted(findings, key=lambda f: f.confidence, reverse=True)
    kept: list[Finding] = []
    for candidate in ordered:
        if candidate.unstructured:  # no line to match on; pass through untouched
            kept.append(candidate.model_copy(deep=True))
            continue
        for existing in kept:
            if _is_duplicate(existing, candidate, line_window):
                if candidate.source_agent not in existing.merged_from and candidate.source_agent != existing.source_agent:
                    existing.merged_from.append(candidate.source_agent)
                # Agreement between independent agents is evidence; nudge confidence up, capped.
                existing.confidence = min(1.0, existing.confidence + 0.1)
                break
        else:
            kept.append(candidate.model_copy(deep=True))
    return kept


def rank_and_cap(findings: list[Finding], max_per_category: int = 10, max_total: int = 25) -> list[Finding]:
    ordered = sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], -f.confidence, f.file, f.line))
    per_cat: dict[str, int] = defaultdict(int)
    out: list[Finding] = []
    for f in ordered:
        if per_cat[f.category] >= max_per_category:
            continue
        per_cat[f.category] += 1
        out.append(f)
        if len(out) >= max_total:
            break
    return out


def aggregate(results: list[AgentResult], line_window: int = 3, max_per_category: int = 10, max_total: int = 25) -> list[Finding]:
    all_findings = [f for r in results for f in r.findings]
    merged = merge_findings(all_findings, line_window=line_window)
    return rank_and_cap(merged, max_per_category=max_per_category, max_total=max_total)
