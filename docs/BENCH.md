# Reading the benchmark

The benchmark exists to answer one question per run: *on this code, does
this configuration find real defects without noise, and what does it cost?*
Everything below is in service of not fooling yourself.

## Cases

| source | how | ground truth |
|---|---|---|
| `--suite builtin` | 20 synthetic Python cases: 14 injected defects, 6 clean changes | author-written; an upper bound on itself |
| `--suite crossfile` | 12 cases whose defect is decided by an unchanged second file; 6 defects, 6 clean mirrors | author-written; the only suite that can show what context does |
| `mine --mode recent` | latest N commits of a real repo, assumed clean | an assumption; label the pool |
| `mine --mode fixes` | SZZ-lite: lines a fix removed, blamed to the commit that wrote them | history, noisy both ways (see below) |

Every truth has a `snippet` the loader checks against its line; the dataset
cannot drift from its own answer key.

## Arms

| arm | roster | context | verifier |
|---|---|---|---|
| A | baseline only | no | no |
| B | baseline only | yes | no |
| C | specialists only | no | no |
| D | specialists only | yes | no |
| E | specialists only | yes | yes (drop mode) |
| F | baseline only | yes | yes (drop mode) |

A→B and C→D isolate context; A→C isolates specialisation; B→F and D→E
isolate the verifier. The benchmark always runs the verifier in `drop`
mode because "what would it remove" is the question; the product default
is `flag`.

## Metrics

- **recall (strict)**: truths with a finding on the same file, same
  category, within `line_tol` lines. **loose**: any category.
- **precision**: findings that land on a truth (loose) over all findings.
- **FP/clean**: findings on cases that have no truths.
- **dup**: extra findings on a truth that already has one -- the
  "19 comments for 14 defects" noise plain precision cannot see.
- **drop tp/fp** (E, F): what the verifier removed that was / was not on a
  truth.
- **tokens, agent_ms**: cost. Tokens are summed over agents and verifier;
  `agent_ms` is the sum of backend wall-times, not the parallel wall-clock.
- Intervals are a percentile bootstrap over cases. With N=20 they are wide,
  and they should be.

`--repeats N` runs everything N times and reports mean [min,max]. Models
are not deterministic; a single run is an anecdote. `compare` does the same
across configs; `report` renders archives side by side.

## The verifier's own benchmark

`bench-verifier` replays a labeled candidate pool through the verifier and
scores **per truth**: a truth is lost only if every candidate covering it
was dropped; a dropped true positive whose truth stays covered is dedup
that worked. Groups are keyed by (pool, arm, case) so candidates from
different runs never meet. The first version merged two runs and reported a
30% kill rate that was the verifier correctly removing the same finding
seen twice.

## Humans

`bench --export-labels` writes a shuffled JSONL showing only
file/line/severity/message/reasoning; the key maps each label back to arm,
agent and ground-truth hit. `label-import` folds `yes/no/dup` answers back:
human precision per arm, machine/human disagreements ("bug by another
name", "right line, wrong claim"), and new truths on git-backed cases.

## Known ways this lies

1. Self-authored suites measure whether the harness works, not how a
   reviewer performs in the wild.
2. SZZ truths are line-level and narrow: blame lands on test fixtures and
   `if hasattr(...)` lines (recall is a floor), and several "false
   positives" are the same bug at a line the fix did not touch (precision
   is a ceiling). The miner now skips test files and snaps off trivial
   lines; it is still an approximation.
3. "Merged means clean" is false often enough that the generalist found
   a pre-existing Link-header bug in a pure move-functions commit.
4. Precision 1.00 with a degenerate interval means the sample held no
   false positives to resample, not that there are none.
5. Diff rendering details (line numbers, context lines, where removed
   lines sit) moved results more than architecture did. Check the prompt
   before trusting a delta.
