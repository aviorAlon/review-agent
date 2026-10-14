"""Zero-cost regex baseline ("semgrep-lite").

Exists for two reasons: it is a legitimate backend to run in CI for free,
and it gives the benchmark a non-trivial floor that needs no API key. It
only ever sees hunks (added/removed lines with their line numbers), never
the surrounding file -- so by construction it cannot benefit from the
context engine, which makes it a useful control in the ablation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from review_agent.agents.base import TimedAgent
from review_agent.schemas import Category, ContextBundle, Finding, Hunk, Severity


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    category: Category
    severity: Severity
    message: str


LINE_RULES: list[Rule] = [
    Rule(re.compile(r"shell\s*=\s*True"), Category.SECURITY, Severity.CRITICAL,
         "subprocess with shell=True: command injection if any part of the command is user-controlled"),
    Rule(re.compile(r"\bpickle\.loads?\("), Category.SECURITY, Severity.CRITICAL,
         "pickle deserialization executes arbitrary code if the input is untrusted"),
    Rule(re.compile(r"\byaml\.load\((?![^)]*Loader)"), Category.SECURITY, Severity.WARNING,
         "yaml.load without an explicit safe Loader"),
    Rule(re.compile(r"\b(eval|exec)\("), Category.SECURITY, Severity.CRITICAL,
         "eval/exec on dynamic input"),
    Rule(re.compile(r"\.execute\(\s*(f[\"']|[\"'][^\"']*[\"']\s*%|[^)]*\+\s*\w)"), Category.SECURITY, Severity.CRITICAL,
         "SQL built by string formatting instead of parameters: injection"),
    Rule(re.compile(r"(?i)\b\w*(api_key|secret|password|passwd|token|_key)\s*=\s*[\"'][A-Za-z0-9_\-]{12,}[\"']"),
         Category.SECURITY, Severity.CRITICAL, "hardcoded credential/secret literal"),
    Rule(re.compile(r"open\(\s*os\.path\.join\([^)]*\b(filename|name|path|user_path|fname)\b"), Category.SECURITY,
         Severity.WARNING, "path built from a caller-supplied name without normalisation: path traversal"),
    Rule(re.compile(r"def\s+\w+\([^)]*=\s*(\[\]|\{\})"), Category.LOGIC, Severity.WARNING,
         "mutable default argument is shared across calls"),
    Rule(re.compile(r"range\(\s*len\([^)]*\)\s*\+\s*1\s*\)"), Category.LOGIC, Severity.WARNING,
         "range(len(x) + 1) indexes one past the end"),
    Rule(re.compile(r"if\s+not\s+os\.path\.exists\("), Category.LOGIC, Severity.WARNING,
         "check-then-act on the filesystem (TOCTOU); use an atomic create instead"),
]

EXCEPT_RE = re.compile(r"^\s*except\b.*:\s*$")
PASS_RE = re.compile(r"^\s*pass\s*$")
FOR_RE = re.compile(r"^\s*for\s+(\w+)\s+in\b")
QUERY_IN_LOOP_RE = re.compile(r"\.(query|execute|fetch|find|get)\(")
MEMBERSHIP_IN_LIST_RE = re.compile(r"\bif\b.*\bin\s+\w+_list\b")
GUARD_RE = re.compile(r"\b(is_admin|is_authenticated|check_permission|authorize|has_perm|verify_\w+)\b|raise PermissionError")
LOOP_BODY_SPAN = 6


class HeuristicAgent(TimedAgent):
    def __init__(self, agent_id: str = "heuristic", role: str = "specialist"):
        self.agent_id = agent_id
        self.role = role

    async def _run(self, context: ContextBundle) -> list[Finding]:
        findings: list[Finding] = []
        for hunk in context.hunks:
            findings.extend(self._scan(hunk))
        return findings

    def _finding(self, hunk: Hunk, line: int, rule_cat: Category, sev: Severity, msg: str, why: str) -> Finding:
        return Finding(file=hunk.file, line=line, severity=sev, category=rule_cat, message=msg,
                       reasoning=why, confidence=0.7, source_agent=self.agent_id)

    def _scan(self, hunk: Hunk) -> list[Finding]:
        out: list[Finding] = []
        if len(hunk.added_line_numbers) == len(hunk.added_lines):
            added = list(zip(hunk.added_line_numbers, hunk.added_lines))
        else:  # numbers unavailable: approximate from hunk start
            added = list(enumerate(hunk.added_lines, start=hunk.start_line))

        for no, text in added:
            for rule in LINE_RULES:
                if rule.pattern.search(text):
                    out.append(self._finding(hunk, no, rule.category, rule.severity, rule.message,
                                             f"matched /{rule.pattern.pattern}/"))

        for idx, (no, text) in enumerate(added):
            if EXCEPT_RE.match(text) and idx + 1 < len(added):
                nxt_no, nxt = added[idx + 1]
                if nxt_no == no + 1 and PASS_RE.match(nxt):
                    out.append(self._finding(hunk, no, Category.LOGIC, Severity.WARNING,
                                             "exception swallowed with bare pass", "except: followed by pass"))

        for idx, (for_no, text) in enumerate(added):
            m = FOR_RE.match(text)
            if not m:
                continue
            var = m.group(1)
            for body_no, body in added[idx + 1:]:
                if body_no > for_no + LOOP_BODY_SPAN:
                    break
                if QUERY_IN_LOOP_RE.search(body):
                    out.append(self._finding(hunk, body_no, Category.PERFORMANCE, Severity.WARNING,
                                             "query issued per loop iteration (N+1)", f"inside `for {var}`"))
                elif MEMBERSHIP_IN_LIST_RE.search(body):
                    out.append(self._finding(hunk, body_no, Category.PERFORMANCE, Severity.INFO,
                                             "list membership test inside a loop is O(n*m); use a set", f"inside `for {var}`"))
                elif "lambda" in body and re.search(rf"\b{re.escape(var)}\b", body.split("lambda", 1)[1]):
                    out.append(self._finding(hunk, body_no, Category.LOGIC, Severity.WARNING,
                                             "lambda closes over the loop variable; every callback sees the last value",
                                             f"late-binding closure over `{var}`"))

        removed_guard = any(GUARD_RE.search(r) for r in hunk.removed_lines)
        if removed_guard and not any(GUARD_RE.search(a) for a in hunk.added_lines):
            at = added[0][0] if added else hunk.start_line
            out.append(self._finding(hunk, at, Category.SECURITY, Severity.CRITICAL,
                                     "an authorization/validation guard was removed and not replaced",
                                     "removed lines matched an auth-check pattern; added lines did not"))
        return out
