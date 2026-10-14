# Architecture

```
 diff ──► scope ──► context ──► fan-out ──► aggregate ──► verify ──► post
          │          │            │            │            │          │
     exclude/limits  symbols    Agent[]     merge/rank    keep|drop   GitLab/GitHub
     (notes)        + callers   (parallel,  + cap         or flag     (fingerprints,
                    + callees    timeouts)                            summary note)
```

Every stage speaks the same handful of Pydantic types in `schemas.py`:
`Hunk`, `ContextBundle`/`SymbolRef`, `Finding`, `AgentResult`. A new backend,
language or git host only has to produce or consume those.

## Stages

| stage | module | what it does | rule it keeps |
|---|---|---|---|
| parse | `diff.py` | unified diff → `Hunk`s with target-side line numbers, context lines, and the anchor each removed line followed | position is meaning: a deleted guard renders above the line it protected |
| scope | `pipeline.scope_hunks` | `exclude:` globs, size and prompt caps | whatever is cut is listed under "Not reviewed" |
| context | `context/` | enclosing function, numbered code around call sites, bodies of project-defined callees | Python via `ast`; JS/TS/Go/Java via tree-sitter; others reported as diff-only |
| fan-out | `orchestrator/orchestrator.py` | every agent runs on the same bundle, concurrently, with its own timeout | a failing backend yields an `AgentResult` with `error`; it never cancels the rest |
| aggregate | `orchestrator/dedup.py` | same file + category within `line_window` → merge; rank by severity then confidence; cap | deterministic, so the benchmark can attribute gains to the agents |
| verify | `agents/verifier.py` | second model pass over the candidate list with the diff | fail-open: only an explicit `drop` removes; errors keep everything |
| post | `providers/` | inline comments + one summary | fingerprints make re-runs idempotent; summary updated in place |

## Roles

Agents carry a `role`: `baseline` (one generalist reviewer), `specialist`
(category reviewers, opt-in), `verifier`. The product default is one
baseline + one verifier. The benchmark's arms select by role, never by
position in the config -- the first harness took `agents[:1]` and made a
security specialist the "baseline", which fabricated a 0.43 → 1.00 jump.

## What is deliberately simple

- Aggregation is rules, not a model, so the ablation can attribute gains.
- Callers are found by grep (`name(`), not a call graph; the snippet around
  the call is what the reviewer needs, the graph is not.
- The verifier downgrades by default (`flag`) instead of deleting. It has
  deleted a true finding on real code once; a wrong downgrade costs nothing.
- No telemetry, no server, no plugin: the CLI reads a diff and a checkout
  and prints or posts. CI integration is one shell step.

## Where the evidence lives

`bench-results/` holds every archived run and the README's Results section
interprets them. Numbers are reproducible from `bench`, `report`,
`compare` and `bench-verifier`; see `docs/BENCH.md`.
