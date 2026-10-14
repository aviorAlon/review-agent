"""Wraps an arbitrary external coding-agent CLI as a backend.

This is the concrete proof of "any agent can plug in": we don't require the
external tool to know about review-agent at all. We feed it the context as
JSON on stdin and a reviewing instruction, then try to parse its stdout as
our Finding schema; if it just prints free text (most CLI agents, by
default), we fall back to wrapping the whole output as one low-confidence
finding rather than discarding it. A real integration tightens this by
giving the external agent a system prompt asking for our JSON shape (see
`extra_args`), but the fallback means it still produces *something* usable
with zero cooperation from the other side.

Example: Claude Code headless -->
    ExternalCLIAgent(
        agent_id="claude-code",
        command=["claude", "-p"],
        category=Category.LOGIC,
    )
"""
from __future__ import annotations

import asyncio
import json

from review_agent.agents.base import TimedAgent
from review_agent.agents.litellm_agent import CATEGORIES, _extract_json_array, _render_user_prompt
from review_agent.schemas import Category, ContextBundle, Finding, Severity, parse_category

REVIEW_INSTRUCTION = (
    "Review the following code change. If you find real issues, respond with "
    "ONLY a JSON array of objects shaped like "
    '{"file": str, "line": int, "category": ' + CATEGORIES + ', '
    '"severity": "critical"|"warning"|"info", '
    '"message": str, "reasoning": str, "confidence": 0-1}. '
    "If you can't produce that shape, just describe what you see.\n\n"
)


class ExternalCLIAgent(TimedAgent):
    def __init__(
        self,
        agent_id: str,
        command: list[str],
        category: Category = Category.OTHER,
        cwd: str | None = None,
        timeout_s: float = 120.0,
        role: str = "specialist",
    ):
        self.agent_id = agent_id
        self.command = command
        self.category = category
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.role = role

    async def _run(self, context: ContextBundle) -> list[Finding]:
        if not context.hunks:
            return []

        prompt = REVIEW_INSTRUCTION + _render_user_prompt(context)

        proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=self.timeout_s
        )
        if proc.returncode != 0:
            raise RuntimeError(f"{self.command[0]} exited {proc.returncode}: {stderr.decode(errors='ignore')[:500]}")

        return self._parse_output(stdout.decode("utf-8", errors="ignore"))

    def _parse_output(self, text: str) -> list[Finding]:
        raw = _extract_json_array(text)
        try:
            if raw is None:
                raise ValueError("no JSON array in output")
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError("not a list")
        except (json.JSONDecodeError, ValueError):
            if not text.strip():
                return []
            # Fallback: the external agent didn't speak our schema. Keep its raw
            # output for the audit trail, but flag it so it is never scored or
            # posted inline -- it has no line to land on.
            return [
                Finding(
                    file="UNKNOWN",
                    line=0,
                    severity=Severity.INFO,
                    category=self.category,
                    message=text.strip()[:400],
                    reasoning="Raw output from an external agent that did not follow the JSON contract.",
                    confidence=0.2,
                    source_agent=self.agent_id,
                    unstructured=True,
                )
            ]

        findings = []
        for item in items:
            try:
                findings.append(
                    Finding(
                        file=item["file"],
                        line=int(item.get("line", 0)),
                        severity=Severity(item.get("severity", "info")),
                        category=parse_category(item.get("category"), self.category),
                        message=item["message"],
                        reasoning=item.get("reasoning", ""),
                        confidence=float(item.get("confidence", 0.5)),
                        source_agent=self.agent_id,
                    )
                )
            except (KeyError, ValueError):
                continue
        return findings
