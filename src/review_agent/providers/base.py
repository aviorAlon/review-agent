"""Output adapters: push findings onto a merge/pull request.

This is the only layer that knows which git host it talks to. The pipeline
upstream is diff-in / findings-out, so adding a host is purely additive --
none of the three bets (context, orchestration, pluggable agents) change.

Both hosts reject inline comments whose line is not part of the diff, so
every provider first partitions findings into "inline-eligible" (file+line
falls inside a hunk) and "summary-only" (everything else, including the
`file="UNKNOWN"` fallback findings from external CLI agents). The summary
ones are posted as one general note rather than dropped.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from review_agent.schemas import Finding, Hunk

# Idempotency. Every posted body carries an invisible HTML comment with the
# finding's fingerprint; a re-run lists what is already on the MR/PR, skips
# those, and updates the single summary note instead of adding another.
FP_PREFIX = "<!-- review-agent:fp="
_FP_RE = re.compile(re.escape(FP_PREFIX) + r"([0-9a-f]{16}) -->")
SUMMARY_MARKER = "<!-- review-agent:summary -->"


def fingerprint(f: Finding) -> str:
    key = f"{f.file}|{f.line}|{f.category.value}|{f.message.strip()[:80]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def fp_marker(f: Finding) -> str:
    return f"{FP_PREFIX}{fingerprint(f)} -->"


def extract_fingerprints(bodies: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for body in bodies:
        found.update(_FP_RE.findall(body or ""))
    return found


@dataclass
class PostResult:
    inline_posted: int = 0
    summary_posted: int = 0
    skipped_existing: int = 0   # already on the MR/PR from an earlier run
    summary_updated: bool = False
    errors: list[str] = field(default_factory=list)


def partition_findings(findings: list[Finding], hunks: list[Hunk]) -> tuple[list[Finding], list[Finding]]:
    """Split into (inline-eligible, summary-only) based on hunk line ranges."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for h in hunks:
        ranges.setdefault(h.file, []).append((h.start_line, h.end_line))

    inline: list[Finding] = []
    summary: list[Finding] = []
    for f in findings:
        spans = ranges.get(f.file, [])
        if not f.unstructured and f.line > 0 and any(lo <= f.line <= hi for lo, hi in spans):
            inline.append(f)
        else:
            summary.append(f)
    return inline, summary


def format_finding(f: Finding) -> str:
    agents = f.source_agent
    if f.merged_from:
        agents += f" (+{', '.join(f.merged_from)} agree)"
    label = f"[{f.severity.value.upper()}{' · flagged' if f.flagged else ''}] {f.category.value}"
    lines = [f"**{label}** — {f.message}"]
    if f.reasoning:
        lines.append("")
        lines.append(f.reasoning)
    if f.flagged:
        lines.append("")
        lines.append(f"⚑ verifier downgraded this: {f.dropped_reason}")
    lines.append("")
    lines.append(f"<sub>review-agent · {agents} · confidence {f.confidence:.2f}</sub>")
    lines.append(fp_marker(f))
    return "\n".join(lines)


def format_summary(summary_findings: list[Finding], inline_count: int, notes: list[str] | None = None) -> str:
    parts = [SUMMARY_MARKER, "## review-agent", f"{inline_count} inline finding(s) posted."]
    if summary_findings:
        parts.append("")
        parts.append(f"{len(summary_findings)} finding(s) could not be placed on a diff line:")
        parts.append("")
        for f in summary_findings:
            if f.unstructured:
                parts.append(f"- _(unstructured output from `{f.source_agent}`)_ — {f.message}")
                continue
            loc = f"`{f.file}:{f.line}`" if f.file != "UNKNOWN" else "_(no location)_"
            parts.append(f"- {loc} **[{f.severity.value.upper()}] {f.category.value}** — {f.message}")
    if notes:
        parts.append("")
        parts.append("Not reviewed:")
        parts.extend(f"- {n}" for n in notes)
    return "\n".join(parts)


class VCSProvider(ABC):
    @abstractmethod
    def post_findings(self, findings: list[Finding], hunks: list[Hunk], notes: list[str] | None = None) -> PostResult:
        raise NotImplementedError
