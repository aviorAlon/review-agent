English | [中文](README.zh-CN.md)

# review-agent

A multi-agent orchestration + deep-context enhancement layer for AI code
review. It treats existing reviewers (PR-Agent, Claude Code, any CLI agent)
as *pluggable backends*, runs them in parallel against one shared structural
context, and merges their findings deterministically.

It is not a CodeRabbit competitor. It is the three things the open-source
field is still missing, built so each one can be validated in isolation:

| Bet | Where it lives | Status |
|---|---|---|
| Deep context (not diff-only) | `context/` | Python via stdlib `ast`; other languages are an extension point |
| Multi-agent fan-out + rule-based aggregation | `orchestrator/` | Working, deterministic, auditable merges |
| Pluggable agent backends | `agents/` | LLM (LiteLLM), external CLI, PR-Agent baseline, fake |

## Five minutes, no API key

```bash
pip install -e .
review-agent demo --repo . --diff-file tests/fixtures/sample.diff   # the pipeline, deterministic backends
review-agent bench                                                  # the benchmark, zero-cost heuristic reviewer
```

The first shows a review end to end; the second scores a regex reviewer
against ground truth and prints recall, precision and confidence intervals.
Nothing leaves your machine.

## With a model

```bash
cp .review-agent.example.yaml .review-agent.yaml     # pick a model / proxy, set the env var it names
review-agent review --base main                      # local run, prints findings
review-agent review --base main --json               # machine-readable
review-agent review --base main --post gitlab --fail-on-critical   # in CI: post inline, gate the MR
```

Dropping this into Jenkins / GitHub Actions / GitLab CI is one shell step.
That *is* the CI integration; there is no plugin to install.

## What is sent to the model

For every review the model receives:

- the diff of the changed files (added, removed and unchanged lines inside
  each hunk, with line numbers), after `exclude:` patterns and `limits:`
  are applied;
- with the context engine on (default): for each changed function, its
  signature, the names it calls, **numbered source lines around each call
  site elsewhere in the repository**, and **the bodies of project-defined
  functions it calls** -- unchanged code the diff never touched;
- the free-form `rules:` from the config, verbatim.

Nothing else: no file listing, no git history, no environment. `--no-context`
sends the diff alone. Everything goes to whatever `api_base` the config
names -- a vendor, or your own proxy -- and nowhere else; this tool has no
telemetry. Keep that paragraph in mind before pointing it at a repository
whose callers you may not send to a third party.

## Verifier: flag, not delete

A second-stage verifier re-reads every finding against the diff. By default
(`verifier_mode: flag`) a negative verdict downgrades the finding to INFO
and annotates it with the reason; the finding still reaches the author and
never blocks CI. `drop` removes it instead. Flag is the default because on
real code the verifier has deleted a true finding once (see Results); the
benchmark always measures drop mode, because that is the question it asks.

## Cost guardrails

`limits:` caps files, hunks, hunk size and total prompt size; `exclude:`
skips vendored, generated, lock and minified files. Whatever is cut is
listed under "Not reviewed" in the posted summary -- a review that quietly
looked at half the PR is worse than one that says so.

## Posting to the MR / PR

`--post auto` sniffs the host from the env; `--post gitlab|github` forces it.

| Host | Env vars read | What gets posted |
|---|---|---|
| GitLab | `CI_PROJECT_ID`, `CI_MERGE_REQUEST_IID`, `CI_SERVER_URL`, **`GITLAB_TOKEN`** | one Discussion per inline finding + one Note as summary |
| GitHub | `GITHUB_REPOSITORY`, `GITHUB_REF`, `GITHUB_API_URL`, **`GITHUB_TOKEN`** | one PR Review carrying all inline comments, summary in its body |

```yaml
# .gitlab-ci.yml
review:
  script:
    - pip install -e .
    - review-agent review --base $CI_MERGE_REQUEST_DIFF_BASE_SHA --post auto --fail-on-critical
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

Both hosts reject inline comments on lines outside the diff. Findings are
first partitioned by hunk range: in-range ones go inline, everything else
(between-hunk lines, files not in the diff, the `UNKNOWN` fallback from
external CLI agents) lands in the summary note instead of being dropped.
On GitLab an inline post the API rejects is demoted to the summary too.

`GITLAB_TOKEN` must be a project/personal access token with `api` scope;
`CI_JOB_TOKEN` cannot create discussions.

## How a review runs

```
diff ──> parse_diff ──> ContextEngine ──> Orchestrator ──> aggregate ──> findings
             │              │                  │               │
          Hunk[]     ContextBundle       AgentResult[]    merge/rank/cap
                     (hunks + symbols   (one per backend,  (rule-based,
                      + repo rules)      errors kept)       no LLM)
```

- Context is built **once** and shared; every agent sees identical input.
- A backend that times out or crashes returns an `AgentResult` with
  `error` set. It never cancels the others and never silently vanishes.
- Duplicates = same file, same category, within `line_window` lines. The
  higher-confidence finding survives; the loser's agent id is recorded in
  `merged_from`, so merges can be audited later instead of guessed at.

## Ablation switches (for the validation study)

| Arm | Command |
|---|---|
| A: baseline, diff only | one `llm` agent in yaml, `--no-context` |
| B: + context | same agent, drop `--no-context` |
| C: + orchestration, diff only | full roster, `--no-context` |
| D: full | full roster |

Every finding carries `source_agent` and `merged_from`, so results can be
blind-labelled with provenance hidden and re-attributed afterwards.

## Adding a backend

Subclass `agents.base.TimedAgent`, implement `_run(context) -> list[Finding]`,
and register a `type` in `config.build_agents`. The orchestrator never sees
anything but `ReviewAgent.review()`.

To wrap an external CLI agent you don't even need code: add a `type: cli`
entry to the yaml. It receives the diff + structural context on stdin.
Structured JSON is preferred; free text is tolerated and wrapped as one
low-confidence finding rather than dropped.

## Adding a language to the context engine

Implement `find_enclosing_symbol(source, line)` and
`find_callers(repo_root, name, exclude_file)` for the language (tree-sitter
is the obvious backing) and dispatch on extension in
`context/engine.py`. Agents only ever see `SymbolRef`, so nothing
downstream changes.

## Benchmark (the measuring, not just the pipeline)

```bash
review-agent bench                                   # zero-cost heuristic roster, arms A-D
review-agent bench --config .review-agent.yaml       # real roster (needs an LLM key)
review-agent bench --arms A,D --json
review-agent bench --export-labels labels.jsonl      # + labels.jsonl.key.json
```

`bench/cases.py` holds 20 tiny before/after repos: 14 with one injected
defect each (6 security, 6 logic, 2 performance) and 6 clean changes.
Diffs are generated from the two states, never hand-written. Every truth
has a `snippet` the loader checks against its claimed line, so the dataset
cannot drift from its own answer key.

Scoring per truth, tolerance 3 lines: *strict* = same file + category;
*loose* = same file, any category. Precision uses loose matches. Clean
cases exist so precision means something: every finding on one is a false
positive. Intervals are a percentile bootstrap over cases.

### Results so far

Heuristic agent (zero cost): recall 0.86 [0.62, 1.00], precision 1.00,
0/6 clean-case FPs, identical across arms because it never reads context.
Misses `logic-none-deref` and `logic-stale-closure` -- semantics regex
can't see. Read it as an upper bound on itself: rules and cases share an
author.

#### Run 1 (superseded): the harness lied

`bench-results/2026-10-13-gpt-5.5-proxy.json`. Arm A was `agents[:1]`,
which silently made the *security specialist* the baseline. It scored
0.43 strict recall against the specialists' 1.00 -- a number that measured
roster composition, not orchestration. Categories were also bound to the
agent rather than classified per finding, so dedup compared agent
identities. Both are fixed; the run is kept as a record of the mistake.

#### Run 2: generalist baseline vs specialists, same model

`gpt-5.5` via proxy, roles declared, categories classified by the model.
`bench-results/2026-10-13-gpt-5.5-proxy-v2-roles.json`.

| arm | roster | recall (strict) | precision | FP/clean | findings | dup | agent-s |
|---|---|---|---|---|---|---|---|
| A | generalist, no ctx | 0.93 [0.77, 1.00] | 1.00 | 0/6 | 13 | 0 | 80 |
| B | generalist, + ctx | 1.00 | 1.00 | 0/6 | 15 | 1 | 77 |
| C | 3 specialists, no ctx | 0.93 | 0.94 | 1/6 | 17 | 3 | 182 |
| D | 3 specialists, + ctx | 1.00 | 1.00 | 0/6 | 17 | 3 | 170 |

Generalist category accuracy on hits: 13/13. Line accuracy: A 11/13
exact, D 12/17 exact (+4 off by one). 200 calls, zero errors.

- **"Multi-agent wins" is not supported.** One generalist call matches the
  three specialists on recall, with fewer findings, fewer duplicates, no
  clean-case FP, and 45% of the agent-time. Category-split specialists
  bought noise and cost on this dataset, nothing else. The PRD's parallel-
  specialist architecture is, on this evidence, the wrong decomposition
  for recall.
- **Context flipped the same case in both pairs.** A→B and C→D each gain
  exactly `logic-none-deref`. Two independent observations on one truth,
  single run, model-default temperature: suggestive, not evidence. Repeat
  runs and cross-file cases are what would make it evidence.
- **Model-classified categories fixed the measurement, not the
  duplicates.** Specialists still label the same line differently and
  defensibly (TOCTOU: security *and* logic), so the rule-based merge still
  folds nothing. Duplicates are a property of specialist rosters. The
  generalist produces almost none.
- **So the live question changed.** It is no longer "do specialists +
  verifier (E) beat specialists (D)" but "does generalist + context +
  verifier (F) beat generalist + context (B)". Arm F exists for that; E
  remains to show whether a verifier can clean a specialist roster's
  duplicates.
- Still true: precision 1.00 is bounded by trivially clean cases; cost is
  time only; N is 20 and self-authored.

#### Run 3: verifier arms, and B/D re-sampled in the same run

`bench-results/2026-10-13-gpt-5.5-proxy-v3-verifier.json`. Same roster
plus a `gpt-5.5` verifier. Arm E = specialists + verifier, F = generalist
+ verifier (the product-shaped arm). 200 calls, zero errors.

| arm | roster | recall (strict) | precision | FP/clean | findings | dup | drop tp/fp | agent-s |
|---|---|---|---|---|---|---|---|---|
| B | generalist, + ctx | 1.00 | 1.00 | 0/6 | 14 | 0 | - | 75 |
| D | 3 specialists, + ctx | 0.93 | 0.94 | 1/6 | 17 | 2 | - | 186 |
| E | specialists + verifier | 0.93 | 1.00 | 0/6 | 15 | 1 | 0/0 | 189 |
| F | generalist + verifier | 1.00 | 1.00 | 0/6 | 14 | 0 | 0/0 | 109 |

- **The verifier dropped nothing in 40 case-runs.** E's gap to D is
  sampling variance of the specialists (independent samples), not
  verifier action. Probed directly on `logic-toctou`: it ran, returned
  `keep` with a correct, substantive reason, and killed no true positive.
  It was inert because the arms handed it nothing false to remove. An
  end-to-end arm is the wrong instrument for a precision stage; see the
  pool replay below.
- **Generalist vs specialists held across two runs.** B: 1.00 / 1.00 both
  runs (15→14 findings, 1→0 dup). D: 1.00→0.93 (lost `logic-toctou` this
  time), 3→2 dup, and the clean-case FP reappeared. Two samples each, but
  the generalist is both cheaper and steadier.
- **The specialists' clean-case FP is reproducible and has a face.** Both
  times it is the *performance* specialist, both times on
  `clean-extract-helper`: "per-iteration function-call overhead" for an
  extracted `line_total(i)` helper, severity INFO. A specialist with
  nothing in its lane still produces something. The generalist never did,
  in four samples.
- **Dedup does merge when the labels agree.** The TOCTOU probe had both
  specialists classify it `security`, so the rule folded them
  (`confidence 1.00` = agreement bonus). Run 2's "merged zero" was partly
  label disagreement that varies run to run, not a dead code path.
- **Context, re-tested on the one case that carried the signal.**
  `logic-none-deref` was found by every with-context arm across runs 2
  and 3 (B×2, D×2) and by neither no-context arm in run 2 (A, C). Still
  one truth; now six consistent observations.

#### Run 4: hunk context lines in the prompt

`bench-results/2026-10-13-gpt-5.5-proxy-v4-context-lines.json`. Until
this run the prompt showed only added/removed lines; unchanged lines inside
the hunk were invisible, so a reviewer could not see `s += prices[i]`
under a changed `range(len(prices) + 1)`. Now the target side is rendered
in line order (`+` added, `' '` unchanged, numbers on both).

| run | arm | recall (strict) | precision | findings | dup | exact-line TPs | agent-s |
|---|---|---|---|---|---|---|---|
| 2 (no ctx lines) | B | 1.00 | 1.00 | 15 | 1 | 11/13 | 77 |
| 3 (no ctx lines) | B | 1.00 | 1.00 | 14 | 0 | 12/15 | 75 |
| **4 (ctx lines)** | B | 0.93 | 1.00 | 13 | 0 | **12/13** | 72 |
| 3 (no ctx lines) | F | 1.00 | 1.00 | 14 | 0 | - | 109 |
| **4 (ctx lines)** | F | 0.93 | 1.00 | 13 | 0 | 11/13 | 92 |

- Run 4 lost `logic-none-deref` in both arms. At the time this read as the
  context signal weakening. It was not: see run 5.

#### Run 5: removed lines anchored to their original position

Run 4's rendering listed removed lines *after* every target-side line, so
a deleted `if user is None:` appeared below the `return user.email` it had
protected and read as unreachable code. The generalist, correctly reading
a wrong picture, stopped flagging it. The verifier pool replay under the
same rendering made it unmistakable: 7 truths lost, 6 of them this case,
every reason "the removed check was after the return, so unreachable".
Each removed line now carries the target line it followed and renders
there. `bench-results/2026-10-13-gpt-5.5-proxy-v5-anchored.json`.

| run | arm | recall (strict) | precision | findings | dup | exact-line TPs | agent-s | tokens |
|---|---|---|---|---|---|---|---|---|
| 5 (ctx, anchored) | B | **1.00** | 1.00 | 15 | 1 | **14/15** | 96 | 10,715 |
| 5 (ctx, anchored) | F | **1.00** | 1.00 | 15 | 1 | 14/15 | 99 | 17,657 |

- `none-deref` is back in both arms: the with-context tally on it is 8/9,
  and the one miss has a named cause. Still one truth; still not proof.
- Best line accuracy so far (14/15 exact) -- the context lines do what
  they were for once they stop lying about order.
- First token numbers: ~540 tokens per case for the generalist; the
  verifier adds ~65% tokens and ~3 s per case, and on this dataset
  removes nothing because there is nothing to remove. That is the honest
  price of F over B until the input has noise.
- The verifier pool replay, three renderings, same 112 groups / 122 candidates
  (`bench-results/2026-10-13-verifier-pool-eval*.txt`):

  | rendering | FP dropped | dup removed | truths lost | errors |
  |---|---|---|---|---|
  | added/removed only | 2/2 | 3 | 1 ("no loop body shown") | 0 |
  | + context, removed at end | 2/2 | 3 | **7** (guards "unreachable") | 0 |
  | + context, removed anchored | 1/2 | 3 | **0** | 0 |

  Zero truths lost once the diff is rendered honestly. The one FP it kept
  this time is the same performance nit it dropped twice before -- 2
  items is a sample of variance, not a rate. Fail-open held throughout:
  nothing was ever removed by error, only by (sometimes misinformed)
  judgment.

`review-agent bench-verifier --pool labels.jsonl --config ...` replays a
labeled candidate pool (real first-stage output with ground-truth `is_tp`)
through the verifier. Scored **per truth**: a truth is lost only if every
candidate covering it is dropped; a dropped TP whose truth stays covered is
dedup that worked. Groups are keyed by (pool, arm, case) so candidates from
different runs never meet -- the first version merged two runs into one
group, the verifier correctly removed the same finding seen twice, and the
scorer reported it as a 30% true-positive kill rate.

Result on the run-2 + run-3 pools (`bench-results/2026-10-13-verifier-pool-eval.txt`),
replayed with the pre-run-4 prompt (no context lines):

| groups | candidates | FP dropped | dup TPs removed | truths lost | precision |
|---|---|---|---|---|---|
| 112 | 122 (120 tp / 2 fp) | **2 / 2** | 3 | **1 / 110 (1%)** | 0.98 → 1.00 |

- Both false positives (the performance specialist's "helper-call overhead"
  nit) were dropped as "speculative micro-optimization, no evidence the
  path is hot" -- the right reason.
- The one lost truth is `logic-off-by-one`: *"no loop body is shown, so
  there is no visible evidence that i is used to index prices."* That is
  the context-line gap run 4 fixed, not a judgment error. Re-replay with
  context lines is the check.
- Dedup removed 3 of roughly 7 in-run duplicate pairs; the pairs it kept
  are two distinct real observations on one line (injection + None
  handling), which is the defensible call.
- Only 2 false positives existed to drop. 2/2 is a direction, not a rate.
  The verifier's benchmark needs noisier input -- real clean PRs -- before
  the drop rate means anything.

`--export-labels` writes a shuffled JSONL showing only file/line/severity/
message/reasoning, plus a key mapping each `label_id` back to arm, source
agents, and ground-truth hit. Label blind, then re-attribute.

### Cross-file suite: the first real test of context

`--suite crossfile`: 12 cases, each a diff that touches ONE file and looks
benign on its own. Whether it is a defect is decided by an unchanged
second file -- a caller handing it `request.args`, a callee named
`safe_join` that does not sanitise, a compliance comment at a call site.
Six defects, each with a clean mirror: same diff, the other file makes it
safe. A diff-only reviewer cannot tell the pairs apart, so context should
raise recall on one half and lower false positives on the other.

For this the context engine was deepened from "callers: file:line" to
numbered code around each call site plus the bodies of project-defined
callees, and the enclosing-symbol lookup now tries the changed lines first
(with three lines of leading context, a hunk's start usually sits above
the function).

Generalist, two runs (`bench-results/2026-10-14-crossfile-A-vs-B*.json`):

| arm | run | recall (strict) | precision | FP on clean mirrors | findings | tokens |
|---|---|---|---|---|---|---|
| A, no context | 1 | 0.50 | 0.50 | 4 / 6 | 8 | 7,832 |
| A, no context | 2 | 0.50 | 0.50 | 4 / 6 | 8 | |
| B, + context | 1 | **0.83** | **0.71** | **2 / 6** | 7 | 9,094 |
| B, + context | 2 | **1.00** | **0.75** | **2 / 6** | 8 | |

Per case, both runs agreed on every outcome but one:

| case | A | B | what context showed |
|---|---|---|---|
| fake-safe-join | miss, miss | **hit, hit** | callee body: `safe_join` is a plain `os.path.join` |
| caller-passes-none | miss, miss | **hit, hit** | caller: `db.get(...)  # None once deleted` |
| callee-returns-none | miss, miss | **hit, hit** | callee docstring: returns None on failure |
| caller-threads | hit, hit | miss, hit | generic "global += not thread-safe"; the one unstable outcome |
| untrusted-name, staff-invoice | hit, hit | hit, hit | flagged from the pattern alone |
| allowlisted-name, single-thread, staff-moderates | fp, fp | **clean, clean** | caller validates / is serial / says staff moderate |
| caller-guards-none, callee-raises | fp or clean | fp, fp | shown the guard and the raising callee, still flags the None deref |

- Defects: 6/12 without context, 11/12 with. False alarms on mirrors:
  8/12 without, 4/12 with. Same model, +16% tokens.
- The two false alarms context does not suppress are the same both runs:
  the model keeps a speculative "could be None" claim even when shown the
  code that prevents it. That is a judgment the verifier exists to make;
  on this suite it has not been run against them yet.
- Caveats, sharpened: the author of the cases is the author of the context
  engine, so this is an upper bound on itself; N is 6+6. Two runs agreeing
  on 23 of 24 outcomes says the effect is stable on this suite, not that it
  transfers to real code. The httpx runs below are that test.

### Real repository: encode/httpx

`review-agent mine --repo httpx --mode recent` took the 15 latest non-merge
commits touching Python (10-400 diff lines, ≤6 files) as noise-pressure
cases: in a mature repository, merged changes are assumed clean, so every
finding is a candidate false positive. The assumption is checked below;
it does not survive intact.

Generalist, with context (`bench-results/2026-10-14-httpx-recent-B-F.json`):

| arm | findings / 15 commits | per commit | dropped | tokens |
|---|---|---|---|---|
| B generalist | 3 | 0.20 | - | 66,109 |
| F generalist + verifier | 5 | 0.33 | **0** | 88,166 |

What it said (all of it):

- `_urls.py`: query encoding now leaves `;` unescaped -- parameter
  injection with parsers that treat `;` as a separator. The commit
  deliberately revised the safe set from WHATWG; the behaviour change is
  real, whether it is a defect is a maintainer's call.
- `_urlparse.py`: a bare `%` in credentials is no longer escaped. Same
  commit family, same character: a real change, arguably intended.
- `tests/test_api.py`: `del sys.modules['httpcore']` is unconditional now
  that importing httpx no longer imports httpcore -- `KeyError` when the
  test runs in isolation. Plausible, specific, checkable.
- `_models.py` (a pure "move functions" commit): Link-header parsing uses
  `param.split("=")` without a max split, so a quoted value containing
  `=` drops that parameter and everything after it. A pre-existing bug the
  move carried along -- the kind of thing "assume clean" cannot see.

So on real, mature code the generalist is quiet (one finding per 3-5
commits) and what it raises is substantive enough that "false positive"
needs a human to decide. The verifier, shown five such candidates, kept
all five; that is the right call for at least two of them. Noise pressure
from the generalist on real code is low -- the specialists' run (below) is
where noise was expected.

`--export-labels` wrote the pool for blind labeling
(`httpx-recent-BF-labels.jsonl`); until someone labels it, the per-commit
counts above are the only honest numbers.

The three specialists on the same 15 commits
(`bench-results/2026-10-14-httpx-recent-D-E.json`):

| arm | findings | per commit | by specialist | dropped | tokens |
|---|---|---|---|---|---|
| D specialists | 10 | 0.67 | perf 3 · logic 5 · security 2 | - | 161,195 |
| E specialists + verifier | 6 | 0.40 | perf 2 · logic 3 · security 1 | **0** | 175,202 |

Specialists produced 3.3× the generalist's findings for 2.4× the tokens.
The extra ones are exactly what the synthetic suite predicted: the
performance specialist's "certifi imported unconditionally" and "httpcore
re-imported on every request" (Python caches imports; these are INFO
nits), plus near-restatements of the `%`-in-userinfo finding on three
different lines that the rule-based merge cannot fold. The genuinely new
contribution was one: `_decoders.py`, empty chunks treated as zstd data in
the very commit that set out to handle empty zstd responses -- specific,
on-topic, worth a maintainer's minute.

Then every first-stage finding from both runs was replayed through the
verifier (`bench-results/2026-10-14-httpx-recent-verifier-replay.txt`):
24 candidates, 14 groups, 0 errors, **2 dropped** -- both "local import
on every request", reason *"speculative micro-performance concern about
Python's cached import mechanism"*. Correct. But the live E run had kept
those same two, and the replay kept the specialist D's wording of the
identical nit on the identical lines. On real code the verifier removes
the one obvious-noise category about half the time and flaps between
runs; it removed nothing substantive. (Precision reads 0.00 in that
replay only because these cases carry no truths by construction.)

Track-2 verdict: on mature real code, noise comes from specialists, not
the generalist; the verifier is safe but weak against it; and "merged
means clean" is false often enough that a labeled pool, not an
assumption, has to be the ground truth here.

### Real bugs: SZZ-mined pairs from httpx

`review-agent mine --mode fixes`: commits whose message says they fix
something (tooling/typing/docs excluded), the lines each fix REMOVED,
`git blame` at the fix's parent, and the commit that wrote those lines
becomes the case; the blamed original line numbers are the truths. 15
bug-introducing commits, 27 truths, none written by anyone here.
`bench-results/2026-10-14-httpx-fixes-B-F.json`.

| arm | recall (loose) | precision | findings | truths killed by verifier | tokens / case |
|---|---|---|---|---|---|
| B generalist + ctx | **0.22** [0.07, 0.45] | 0.35 | 17 | - | 8,393 |
| F + verifier | 0.11 [0.00, 0.27] | 0.20 | 15 | **1** | 13,822 |

The six truths the generalist hit are real bugs caught at the commit
that introduced them: IPv6 and CIDR `NO_PROXY` entries misparsed (the
exact later fix, in two commits of that chain), a binary-mode check that
rejects valid file-likes, `Headers.update` dropping duplicate values,
root-relative URLs losing their slash without a base URL, a sync read
inside an async stream. Specific, on the right line, before the bug
shipped.

Why the number is both too low and too high:

- **SZZ truths are line-level and narrow.** The three zero-finding misses
  include truths on *test fixture byte strings* (`test_cookies.py:60`,
  a `Set-Cookie` literal) and on an `if hasattr(self._stream, "read"):`
  line -- where blame landed, not where a reviewer could see a defect.
  Recall against these is a floor.
- **Several "false positives" are the bug at a different line.** On the
  `chunk_size` commit, whose later fix was "iter_text adding an empty
  chunk", the generalist flagged "`iter_bytes()` raises for cached empty
  responses" -- same defect family, lines the fix did not touch. On the
  `Map HTTPCore exceptions` commit it flagged that only the initial
  `transport.request` is wrapped, so exceptions during iteration escape
  unmapped: a real httpx issue fixed much later. On `stream_lines` it
  flagged a bare `\r` split across chunks -- the fix was "match stdlib
  line splitting". Precision against SZZ truths is a ceiling.
- **The verifier killed a real one.** F lost the IPv6 `NO_PROXY` finding
  (`fix-682cad39eb`), its first truth loss on real code after 0/110 on the
  synthetic replay. F's lower recall is mostly an independent first-stage
  sample (15 vs 17 findings) plus that kill; the verifier has now earned
  a hard "flag, don't delete" question for real code.
- Real bug commits are big: 8.4k tokens per case for one generalist call,
  13.8k with the verifier.

Track-3 verdict: the generalist finds roughly a fifth of blamed defects at
introduction time, and what it finds is genuine; the ground truth is
noisy in both directions, so the next step is not more mining but a
human pass over this pool to label which "false positives" are the bug
by another name. Until then, 0.22 is the honest number and its interval
is [0.07, 0.45].

### Three repositories

The same two recipes -- recent commits assumed clean, SZZ-mined bug
pairs -- on `pallets/click` and `psf/requests` (10 cases each, arms B and F,
`bench-results/2026-10-14-{click,requests}-*`), next to httpx:

| repo | bug pairs: truths | B loose recall | B findings | recent: findings / commit | verifier drops (any arm) | tokens / bug case |
|---|---|---|---|---|---|---|
| httpx | 27 | 0.22 | 17 | 0.20 | 0 | 8.4k |
| requests | 18 | 0.11 | 9 | 0.00 | 0 | 3.3k |
| click | 21 | 0.00 (F: 0.14) | 14 | 0.50 | 0 | 7.3k |

What the per-finding view adds to the table:

- On click the generalist's "misses" include *"Windows launch path tries to
  execute the shell built-in `start` with shell=False"*, *"Popen is called
  with a list while shell=True"* and *"_tempfilepager drops pager command
  arguments"* on the commit chain whose later fix is titled *"Fixes windows
  specific error"* -- the bug, on lines the fix did not touch. Same pattern
  as httpx: line-level SZZ truth is a floor on recall.
- On requests the two hits are a `self.error` / `self._error` mismatch at
  the introducing commit and missing-encoding handling in streaming decode.
  Among the "false positives": chunked requests bypassing the configured
  timeout, pool connections not returned on failure, gzip errors swallowed.
- Recent-commit findings on click (*temporary pager file opened in text
  mode without the computed encoding*, *`_is_expected_type` compares the
  type of the type argument*) are the kind only a human label can settle.
- **The verifier removed nothing in any live run on any of the three
  repositories.** The only removals ever observed were in replays. Flag
  mode as the default is the right call; its live value on real code is
  unproven, and would need labeled noise to measure.

Across three repositories the honest summary is: the generalist catches a
tenth to a fifth of narrowly-defined historical bugs at the commit that
introduced them, what it says is specific enough to be worth a maintainer's
minute, and every number here is bounded by ground truth nobody has
labeled yet. The pools are exported; `label-import` is waiting.

### Labels

Every exported pool was labeled, reading each finding against the code at
its commit (`bench-results/labels/`: pools, keys, answers, and the cases
with the resulting truths added). **The labeler was the assistant that
built the pipeline**, not an independent maintainer: the labels are
informed, specific and conservative, and they are not blind. An independent
human pass over the same JSONL files is the next thing that would move
these numbers; `label-import` takes the CSV as is.

| pool | arm | labeled | yes | no | human precision |
|---|---|---|---|---|---|
| httpx bug commits | B | 17 | 14 | 3 | **0.82** |
| httpx bug commits | F | 15 | 12 | 3 | 0.80 |
| requests bug commits | B | 9 | 9 | 0 | **1.00** |
| requests bug commits | F | 9 | 9 | 0 | 1.00 |
| click bug commits | B | 14 | 12 | 2 | **0.86** |
| click bug commits | F | 18 | 15 | 3 | 0.83 |
| httpx recent | B | 3 | 1 | 2 | 0.33 |
| httpx recent | D (specialists) | 10 | 3 | 7 | 0.30 |
| httpx recent | E | 6 | 3 | 3 | 0.50 |
| click recent | B | 5 | 4 | 1 | 0.80 |
| click recent | F | 4 | 3 | 1 | 0.75 |

What the labels change:

- **On bug-introducing commits the generalist's precision is 0.8-1.0 by a
  reader, against 0.2-0.35 by line-level SZZ truth.** 38 findings the
  machine scored as false positives are, on reading, the bug by another
  name (or another real one): `range(..., 0)` on empty cached responses,
  chunked uploads bypassing the timeout, pool connections not returned,
  `TextIOWrapper` closing a borrowed `stdout.buffer` -- the last one is
  the exact defect a later click commit fixes. These now live as truths in
  the labeled case files. They were derived from this run's findings, so
  they must not be used to score *this* run; they are the ground truth for
  the next configuration.
- **On recent commits the misses are intentional behaviour changes.** Both
  httpx "no" labels are the WHATWG percent-encoding revisions the commit
  set out to make; the `%`/`;` findings are accurate descriptions of a
  deliberate change, not defects. The specialists' extra "no" labels are
  the per-request-import nits.
- **Judgment calls labeled `no`, so the numbers lean low:** four httpx
  findings about a removed close-callback on iterator streams (a real
  behaviour change, unclear intent), a fish-completion format claim that
  could not be verified against the shell wrapper, an ellipsis-length nit,
  a `_SkipClose` flush nit, and a private-API-availability nit.

### Three tracks, one page

| question | instrument | answer so far |
|---|---|---|
| Does context help? | cross-file suite | yes, both directions, stable across 2 runs; self-authored |
| Does the verifier drop real-world noise? | httpx recent + replay | ~half of the one obvious category, inconsistently; nothing substantive; kept 24 of 24 live |
| Can the pipeline find real bugs? | httpx SZZ pairs | ~1 in 5 blamed truths, hits are genuine, truth is noisy |
| Is "multi-agent by specialty" worth it? | all three | no: 2.4x tokens for nits and restatements, one extra real catch in 15 commits |

What would move any of these numbers is labeled data, not code.

## Measuring a configuration on your own code

```bash
review-agent mine --repo . --mode fixes  --n 15 --out bugs.json    # commits that introduced later-fixed bugs; truths from git blame
review-agent mine --repo . --mode recent --n 15 --out clean.json   # recent merged commits, assumed clean
review-agent compare --config a.yaml --config b.yaml --cases bugs.json  --arms B,F --repeats 2
review-agent compare --config a.yaml --config b.yaml --cases clean.json --arms B,F --repeats 2
review-agent report bench-results/*.json                           # archives side by side
```

Then have someone label the exported pool and fold it back:

```bash
review-agent bench --config a.yaml --cases bugs.json --arms B --export-labels pool.jsonl
# ... a human fills answers.csv as label_id,yes|no|dup ...
review-agent label-import --pool pool.jsonl --answers answers.csv --cases bugs.json --out bugs-labeled.json
```

`docs/BENCH.md` explains every column and every way the numbers can lie.

## FAQ

**Why one generalist and not specialists?** Same model, the specialists
matched the generalist on recall and added duplicates, a reproducible
performance nit on harmless refactors, and 2.4× the tokens -- on synthetic
and on real commits. Turn them on if a benchmark on *your* code says
otherwise; `compare` is how you find out.

**Why does the verifier not delete?** It has deleted a true finding on real
code once. In `flag` mode a wrong verdict costs an INFO badge; in `drop`
mode it costs the finding.

**My language shows "diff-only context".** Python needs nothing; JS/TS, Go
and Java need `pip install "review-agent[languages]"`; anything else is
reviewed from the diff alone and the summary says so. Adding a language is
two functions: `docs/BACKENDS.md`.

**Can I use my own agent?** Yes: `type: cli`, any command, the contract is
on stdin. Claude Code is verified. PR-Agent as a backend is experimental.

**Where did my MR comment go on re-run?** It is still there; re-runs skip
findings already posted (fingerprints) and update the one summary note.

**What does it cost?** Roughly 4-9k tokens per commit for the generalist
on real repositories, +65% with the verifier; `--json` and `--audit-dir`
report exact counts per run.

## More

- `docs/ARCHITECTURE.md` -- stages, roles, what is deliberately simple
- `docs/CONFIG.md` -- every key, every env var, every command
- `docs/BACKENDS.md` -- adding a backend, a language, a git host
- `docs/BENCH.md` -- reading the benchmark and its failure modes
- `ROADMAP.md`, `CONTRIBUTING.md`, `CHANGELOG.md`

## What is deliberately not here yet

- Posting comments to GitHub/GitLab. PR-Agent's `git_providers` is the
  intended reuse point; local-diff mode comes first because it is what
  the validation study needs.
- LLM-based aggregation. Rules first, so the ablation can attribute gains
  to the generation agents instead of to a clever merger. Upgrading the
  merger is its own arm (E) with its own comparison.
- Real-world cases in the benchmark. The seed set is synthetic and
  self-authored; mined bug-introducing/bug-fixing commit pairs are the
  next data source, and the first one that can say anything about
  performance outside this repo.
