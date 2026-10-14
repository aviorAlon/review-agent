# Configuration reference

`review-agent init` writes a starter `.review-agent.yaml`. The file lives in
the repository under review; changing the roster is a pull request.

## Top level

| key | default | meaning |
|---|---|---|
| `version` | `"1"` | schema version |
| `agents` | generalist + verifier | the roster; see below |
| `aggregation.line_window` | 3 | findings on the same file and category within N lines are merged |
| `aggregation.max_per_category` | 10 | cap per category after ranking |
| `aggregation.max_total` | 25 | overall cap |
| `verifier_mode` | `flag` | `flag`: negative verdict downgrades to INFO with the reason; `drop`: removes (kept in `--json`/audit) |
| `limits.max_files` | 40 | files reviewed per run; the rest are listed as not reviewed |
| `limits.max_hunks` | 150 | hunks reviewed per run |
| `limits.max_hunk_lines` | 400 | larger hunks are skipped (generated/vendored code) |
| `limits.max_prompt_chars` | 120000 | diff budget (~30k tokens); hunks past it are skipped in order |
| `exclude` | built-in list | globs matched against the path and the basename; omit the key for the default list |
| `per_agent_timeout_s` | 180 | per backend; a timeout yields an error result, never a hang |
| `rules` | `{}` | free-form map handed to every agent verbatim |

## Agents

| key | default | meaning |
|---|---|---|
| `id` | required | provenance label on every finding |
| `type` | required | `llm` (LiteLLM), `cli` (external command), `heuristic` (regex, free), `fake` (deterministic smoke) |
| `role` | `specialist` | `baseline` \| `specialist` \| `verifier` |
| `category` | `other` | fallback when the model omits a category; the model classifies each finding itself |
| `focus` | `""` | what an `llm` agent is told to look for |
| `model` | `gpt-4o-mini` | LiteLLM model string; `openai/<name>` for any OpenAI-compatible endpoint |
| `api_base` | none | e.g. `https://your-proxy/v1` |
| `api_key_env` | none | NAME of the env var holding the token; the value never lives in yaml; missing var fails before any call |
| `temperature` | 0 | `null` to omit (reasoning models reject it; `drop_params` is always on) |
| `max_findings` | 10 | per agent, before aggregation |
| `command` | `[]` | for `cli`: argv; the prompt (diff, context, JSON contract) arrives on stdin |
| `enabled` | true | |

### Backends by type

- `llm`: one chat completion per review with a JSON-array contract; category,
  line and confidence are the model's. Token usage is recorded.
- `cli`: any executable. Structured JSON on stdout is preferred; free text
  is kept as an `unstructured` finding (audit only, never scored or posted
  inline). Verified with Claude Code: `["claude", "-p", "<instruction>"]`.
- `heuristic`: ten regex rules and three structural checks; no network.
- `fake`: flags symbols without callers; for tests and `demo`.

## Environment

| variable | used by |
|---|---|
| whatever `api_key_env` names | `llm` agents and verifiers |
| `GITLAB_TOKEN`, `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_SERVER_URL` | `--post gitlab` (token needs `api` scope; `CI_JOB_TOKEN` cannot create discussions) |
| `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `GITHUB_REF`, `GITHUB_API_URL` | `--post github` |
| `REVIEW_AGENT_CACHE` | where `mine`/`bench` cache git archives (default `~/.cache/review-agent`) |

## CLI

```
review-agent init                              write a starter config
review-agent review [--base REF | --diff-file F] [--post none|auto|gitlab|github]
                    [--fail-on-critical] [--no-context] [--json] [--audit-dir DIR]
review-agent demo                              pipeline with deterministic backends, no key
review-agent bench [--config] [--suite builtin|crossfile|all | --cases F] [--arms A-F]
                   [--repeats N] [--concurrency N] [--json] [--export-labels F]
review-agent compare --config a.yaml --config b.yaml [--arms B] [--repeats 2]
review-agent report run1.json run2.json ...
review-agent bench-verifier --pool F --config C [--suite | --cases]
review-agent mine --repo DIR --mode recent|fixes --n N --out F
review-agent label-import --pool F --answers CSV [--cases F --out F]
```
