# Changelog

## 0.1.0 (unreleased)

First public version.

- Pipeline: diff → scope → context → fan-out → aggregate → verify → post.
  One generalist reviewer plus a fail-open verifier by default; category
  specialists opt-in.
- Backends: LiteLLM (any vendor or OpenAI-compatible proxy), external CLI
  agents (Claude Code verified), a free regex reviewer, deterministic fakes.
- Context: Python via `ast`; JavaScript/TypeScript, Go, Java via the
  optional `languages` extra; call-site code and callee bodies, not just
  names.
- Posting: GitLab discussions/notes and GitHub reviews/issue comments,
  idempotent through fingerprints; summary updated in place; "Not reviewed"
  lists what limits and excludes cut.
- Benchmark: synthetic and cross-file suites, git-history mining (recent
  commits, SZZ-lite fix pairs), arms A-F, repeats, `compare`, `report`,
  blind-label export and import, per-truth verifier replay, token and time
  accounting, archived runs for httpx, click and requests.
- Guardrails: prompt/file/hunk caps, exclude globs, per-agent timeouts,
  verifier `flag` mode, audit record per run.
