# Contributing

## Setup

```bash
python -m venv .venv && . .venv/bin/activate      # .venv/Scripts/activate on Windows
pip install -e ".[dev,languages]"
python -m pytest -q
```

The test suite needs no model, no key and no network; it runs on the
heuristic and fake backends and on `httpx.MockTransport`. Keep it that way:
a test that calls a model is a benchmark run, and belongs in
`bench-results/` with its JSON.

## What a change needs

- Tests for behaviour, not just for the happy path. Partial failure is the
  common case here (a backend times out, a host rejects a line, a model
  returns prose); the suite is mostly about those.
- If it touches the prompt, the diff rendering or the context engine: a
  before/after benchmark run (`bench --suite all --repeats 2`) in the PR,
  with the JSON archived. Rendering details have moved results more than
  architecture has.
- If it adds a backend, language or host: see `docs/BACKENDS.md`.
- No secrets, no internal hosts, no machine paths in anything tracked.
  `*.local.yaml` and `.env` are ignored for that reason; result archives
  must carry relative `source` names.

## Benchmark etiquette

- Archive every run you cite: `bench --json > bench-results/<date>-<what>.json`.
- Say which model, which proxy, how many repeats, and what the interval is.
- Prefer `compare` over hand-built tables; prefer `report` over copying
  numbers into prose.
- When a number looks good, look for the harness bug first. This project's
  history has four of them.

## Style

Default to no comments; when one is needed it says *why*. Short functions,
plain data classes, no abstractions for hypothetical futures. Keep the
public shapes in `schemas.py` stable: everything plugs into them.
