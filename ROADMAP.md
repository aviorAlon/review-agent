# Roadmap to open source

Priority: **P0** = unsafe or unusable for a stranger without it; **P1** = the
differentiator (measurement) does not stand without it; **P2** = decency.
Effort: S < half a day, M = a day or two, L = a week.

Items marked **needs you** require an account, token or decision that the
maintainer must supply; everything else is prepared in the repo.

## P0 -- do not publish without

- [x] 1. Scrub internal hosts and paths from tracked files and result
      archives (S). *Note: early commits in this repository's history still
      contain the internal proxy host; publish from a squashed history or run
      `git filter-repo` first -- **needs you** (destructive, your call).*
- [x] 2. License: Apache-2.0 + NOTICE; dependencies are MIT/BSD (S).
- [x] 3. Privacy statement: what is sent to the model, and the switch that
      sends only the diff (S).
- [x] 4. Live verification of GitLab/GitHub posting on a real MR/PR, and
      idempotent posting via finding fingerprints (M). *Live run **needs you**:
      a test repository and a token.*
- [x] 5. Verifier `flag` mode (downgrade, do not delete) as default; the CI
      gate ignores INFO and flagged findings (S).
- [x] 6. Cost guardrails: prompt budget, file and hunk caps, with truncation
      reported in the summary (S).
- [x] 7. Noise-file exclusion: vendored, generated, lock, minified, oversized
      hunks; `exclude:` globs (S).
- [x] 8. Packaging: PyPI metadata, Docker image (built in CI; no local Docker here), `review-agent init`, CI that
      runs the tests without any LLM (M). *PyPI upload **needs you**: a
      project name and a token.*
- [x] 9. Five-minute no-key quickstart leads the README (S).

## P1 -- the measurement story does not stand without

- [x] 10. Label import: human answers on an exported pool become `is_tp` /
      truths, metrics recomputed (M).
- [x] 11. `bench report`: tables from archived JSON, several runs side by side (S).
- [x] 12. `--repeats N` with mean and range -- single runs flap everywhere (S).
- [x] 13. `compare`: N candidate configs on one case set in one command, one
      table (M).
- [x] 14. Miner quality: truths never on test files or trivial lines; archive
      cache; parallel cases (M). *Resumable runs: not done.*
- [x] 15. Context engine beyond Python: JS/TS, Go, Java via tree-sitter;
      honest diff-only fallback elsewhere (L).
- [x] 16. Make the claims true: Claude Code (`claude -p`) verified as an external backend with structured output; the
      PR-Agent backend is labelled
      experimental (M).

## P2 -- decency after launch

- [x] 17. Observability: `--audit-dir` writes one JSON per run (run id, models,
      tokens, every verifier verdict with reason, findings, what was cut);
      `--json` carries the run id. *Prompts are not stored (they contain the
      code); OpenTelemetry not done.*
- [x] 18. Results from a second and third real repository: psf/requests and
      pallets/click mined and run (`bench-results/2026-10-14-*`). *Labeling
      the exported pools **needs you**.*
- [x] 19. Docs: `docs/ARCHITECTURE.md`, `docs/CONFIG.md`, `docs/BACKENDS.md`,
      `docs/BENCH.md`, README FAQ (M).
- [x] 20. CONTRIBUTING, CHANGELOG, issue templates (S). *A name **needs you**;
      candidates checked against PyPI on 2026-10-14, all free: `diffjury`,
      `reviewbench`, `verdictci` (and `review-agent` itself). Update
      `pyproject.toml`, the console script and `src/review_agent/` together.*
- [x] 21. Rough edges: Typer/rich help output crashes on legacy Windows
      consoles when piped (S).

## Still open after P0-P2

- Live posting against a real GitLab/GitHub MR (item 4) and the PyPI upload
  (item 8) wait on credentials.
- Resumable benchmark runs (item 14).
- The exported pools (httpx, requests, click) were labeled by the assistant
  that built the pipeline, reading each finding against the code at its
  commit (`bench-results/labels/`). Those labels are informed but not
  independent: an independent human pass over the same JSONL files is still
  the highest-value action left, and `label-import` takes the CSV as is.
- Repository history contains the internal proxy host in early commits;
  squash or filter before publishing (item 1).

## Suggested order

P0 items 1, 2, 3, 5, 6, 7, 9 are a day together; 4 and 8 a day each. Then
P1 10 and 13 before 15: they decide whether this is a measurement tool or
another bot. 15 is the heaviest and the least urgent.
