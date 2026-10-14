# Writing a backend, a language, or a git host

## A review backend

Subclass `agents.base.TimedAgent` and implement
`async _run(self, context: ContextBundle) -> list[Finding]`. The base
class fills in timing, token usage (set `self.last_usage = (prompt, completion)`
inside `_run` if you know it) and error capture: raise freely, the
orchestrator receives an `AgentResult` with `error` set and the other
agents keep running.

```python
from review_agent.agents.base import TimedAgent
from review_agent.schemas import Category, Finding, Severity

class MyAgent(TimedAgent):
    def __init__(self, agent_id="mine", role="specialist"):
        self.agent_id, self.role = agent_id, role

    async def _run(self, context):
        out = []
        for h in context.hunks:
            for no, line in zip(h.added_line_numbers, h.added_lines):
                if "TODO" in line:
                    out.append(Finding(file=h.file, line=no, severity=Severity.INFO,
                                       category=Category.STYLE, message="TODO left in change",
                                       source_agent=self.agent_id))
        return out
```

Register a `type` for it in `config.build_agents` if you want it in yaml.

What a `Finding` must carry: `file`, `line` (target side, as shown in the
prompt), `severity`, `category`, `message`. `reasoning` and `confidence`
help the verifier and the reader. Never set `source_agent` to anything but
your id; dedup records it in `merged_from` when it folds you into another
finding.

## An external CLI agent (no code)

```yaml
- id: my-agent
  type: cli
  role: baseline
  command: ["my-agent", "--non-interactive"]
```

Your process receives on stdin: an instruction, the diff rendered with
target-side line numbers (`+ 12:` added, `  13:` unchanged, `-` removed at
its original position), the structural context (call sites, callee
definitions), the repo rules, and the JSON contract:

```
[{"file": str, "line": int, "category": "security"|"logic"|"performance"|"test_coverage"|"style"|"other",
  "severity": "critical"|"warning"|"info", "message": str, "reasoning": str, "confidence": 0-1}]
```

Print that array on stdout and exit 0. Anything else is kept as one
`unstructured` finding (audit only). Non-zero exit becomes an error result.
Claude Code works as `["claude", "-p", "...respond with ONLY that JSON array."]`.

## A verifier

Subclass `agents.verifier.Verifier`, implement
`async _run(self, findings, context) -> list[Verdict]`. Return a verdict
per index you want to judge; omitted indices are kept. Only
`Verdict(keep=False)` removes (or, in `flag` mode, downgrades).

## A language for the context engine

Implement two functions and register the extension in
`context/engine.py::_tools`:

- `find_enclosing_symbol(source, line) -> EnclosingSymbol | None`
  (name, line, one-line signature, callee names)
- `find_definition(repo_root, name) -> str | None`
  (`"file:line\n  numbered body"`, truncated)

Callers are found by text search for `name(` across the language family's
extensions, so you also add a branch to `symbol_index._is_definition_line`
that recognises your language's definition syntax -- anchored tightly;
a call statement must never match. `treesitter_index.py` is the worked
example for JS/TS, Go and Java.

## A git host

Subclass `providers.base.VCSProvider`, implement
`post_findings(findings, hunks, notes) -> PostResult`. Use
`partition_findings` (inline vs summary -- hosts reject lines outside the
diff), `format_finding` (carries the fingerprint marker) and
`format_summary` (carries the summary marker). On a re-run, list what is
already posted, skip fingerprints you find, update the summary in place.
Test it against `httpx.MockTransport`; see `tests/test_providers.py`.
