"""A real LLM-backed review agent, via LiteLLM.

LiteLLM gives us one call shape across OpenAI/Anthropic/Gemini/local models
(Ollama, vLLM, ...) -- same gateway choice PR-Agent itself made, so we are
not reinventing provider plumbing. Each instance is one "specialist": its
`focus` decides what it's told to hunt for, and its `model` can be routed
independently (cheap model for routine passes, frontier model for the
high-stakes security/logic specialists).

The *category* of a finding is classified by the model per finding. The
agent's `category` is only the fallback when the model omits it. Binding
category to the agent was a design error: it made "same category" in the
dedup rule mean "same agent", so specialists could never merge.
"""
from __future__ import annotations

import json

from review_agent.agents.base import TimedAgent
from review_agent.schemas import Category, ContextBundle, Finding, Severity, parse_category

CATEGORIES = "|".join(f'"{c.value}"' for c in Category)

SYSTEM_PROMPT_TEMPLATE = """You are a specialist code reviewer focused ONLY on: {focus}

You will be given a diff plus structural context (the enclosing function/class
for each change, and known callers/callees). Only flag issues within your
specialty. If you find nothing in your specialty, return an empty list.

Respond with ONLY a JSON array, no prose, no markdown fences. Each element:
{{"file": str, "line": int, "category": {categories},
  "severity": "critical"|"warning"|"info",
  "message": str, "reasoning": str, "confidence": float between 0 and 1}}

`category` must describe what the defect actually IS, not what your specialty
is. An off-by-one is "logic" even when a performance reviewer reports it.
`line` must be the exact line number shown next to the added line.
"""


def _render_user_prompt(context: ContextBundle) -> str:
    parts: list[str] = ["# Changed hunks  (+ added, ' ' unchanged, - removed; numbers are target-side lines)"]
    for h in context.hunks:
        parts.append(f"\n## {h.file} (lines {h.start_line}-{h.end_line})")
        # True unified order: added and unchanged lines by their target line
        # number, each removed line right after the target line it followed.
        # Listing removed lines at the end once made a deleted None-guard look
        # like unreachable code below the return it used to protect.
        numbered = (len(h.added_line_numbers) == len(h.added_lines)
                    and len(h.context_line_numbers) == len(h.context_lines)
                    and len(h.removed_anchors) == len(h.removed_lines))
        if numbered:
            rows = [(no, 0, i, f"+ {no}: {text}") for i, (no, text) in enumerate(zip(h.added_line_numbers, h.added_lines))]
            rows += [(no, 0, i, f"  {no}: {text}") for i, (no, text) in enumerate(zip(h.context_line_numbers, h.context_lines))]
            rows += [(anchor, 1, i, f"-    {text}") for i, (anchor, text) in enumerate(zip(h.removed_anchors, h.removed_lines))]
            parts.extend(row[3] for row in sorted(rows, key=lambda r: (r[0], r[1], r[2])))
        else:
            parts.extend(f"+ {line}" for line in h.added_lines)
            parts.extend(f"- {line}" for line in h.removed_lines)

    if context.symbols:
        parts.append("\n# Structural context (unchanged code elsewhere in the repo; judge the change by how it is used)")
        for s in context.symbols:
            parts.append(f"\n## {s.signature}  ({s.file}:{s.line})")
            if s.callers:
                parts.append(f"Called from: {', '.join(s.callers)}")
            if s.callees:
                parts.append(f"Calls: {', '.join(s.callees)}")
            for block in s.caller_snippets:
                parts.append(f"\n### Call site {block}")
            for block in s.callee_defs:
                parts.append(f"\n### Definition of a called function, {block}")

    if context.repo_rules:
        parts.append(f"\n# Repo rules\n{json.dumps(context.repo_rules)}")

    return "\n".join(parts)


class LiteLLMAgent(TimedAgent):
    def __init__(
        self,
        agent_id: str,
        focus: str,
        category: Category = Category.OTHER,
        model: str = "gpt-4o-mini",
        max_findings: int = 10,
        api_base: str | None = None,
        api_key: str | None = None,
        temperature: float | None = 0,
        role: str = "specialist",
    ):
        self.agent_id = agent_id
        self.focus = focus
        self.category = category
        self.model = model
        self.max_findings = max_findings
        self.api_base = api_base
        self.api_key = api_key
        self.temperature = temperature
        self.role = role

    async def _run(self, context: ContextBundle) -> list[Finding]:
        import litellm  # imported lazily so the package is optional at runtime

        if not context.hunks:
            return []

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(focus=self.focus, categories=CATEGORIES)
        user_prompt = _render_user_prompt(context)

        # drop_params: reasoning models (gpt-5.x, o-series) reject temperature and
        # LiteLLM raises *before* sending. Dropping per-call keeps the request
        # alive on those models without changing behaviour on the rest.
        extra: dict = {"drop_params": True}
        if self.temperature is not None:
            extra["temperature"] = self.temperature
        if self.api_base:
            extra["api_base"] = self.api_base
        if self.api_key:
            extra["api_key"] = self.api_key
        response = await litellm.acompletion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **extra,
        )
        self.last_usage = _usage_of(response)
        content = response.choices[0].message.content or ""
        raw = _extract_json_array(content)
        items = json.loads(raw) if raw is not None else []

        findings = []
        for item in items[: self.max_findings]:
            findings.append(
                Finding(
                    file=item["file"],
                    line=int(item["line"]),
                    severity=Severity(item.get("severity", "info")),
                    category=parse_category(item.get("category"), self.category),
                    message=item["message"],
                    reasoning=item.get("reasoning", ""),
                    confidence=float(item.get("confidence", 0.6)),
                    source_agent=self.agent_id,
                )
            )
        return findings


def _usage_of(response: object) -> tuple[int, int] | None:
    """(prompt_tokens, completion_tokens) from a LiteLLM response, or None if absent."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return (int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0))
    except (TypeError, ValueError):
        return None


def _extract_json_array(text: str) -> str | None:
    """Models love to wrap JSON in markdown fences despite instructions. Strip it.

    Returns None when no array is present at all, so callers can tell
    "the model emitted nothing structured" apart from a literal empty list.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    return text[start : end + 1]
