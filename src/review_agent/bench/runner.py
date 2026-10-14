"""Ablation harness: run an agent roster over the benchmark under each arm,
score against ground truth, bootstrap confidence intervals, and export a
provenance-stripped pool for blind labeling.

Arms select agents by *role*, never by position:
  A  baseline only, no context      -- the single-agent control
  B  baseline only, with context    -- isolates the context engine
  C  specialists only, no context   -- isolates specialisation/fan-out
  D  specialists only, with context -- the full stack
An arm that selects no agents is an error, not a silent empty run: the
first version took `agents[:1]` and quietly made a security specialist
the "baseline", which produced a 0.43 -> 1.00 jump that measured roster
composition, not orchestration.

Scoring per truth (tolerance in lines):
  strict hit  = some finding with same file, same category, |line| <= tol
  loose hit   = same file, |line| <= tol, any category
Precision is computed on loose matches. `dup` counts surviving findings
beyond the first on an already-hit truth: that is the review-noise the
PRD worried about (19 comments for 14 defects) and plain precision cannot
see it. Findings flagged `unstructured` are excluded from all scoring.

Elapsed is the SUM of agent wall-times (a cost proxy), not the parallel
wall-clock -- tokens scale with the sum.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import random
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from review_agent.agents.base import ReviewAgent
from review_agent.agents.verifier import Verifier
from review_agent.bench.cases import Case
from review_agent.pipeline import run_review
from review_agent.schemas import Finding


@dataclass(frozen=True)
class Arm:
    key: str
    description: str
    roles: frozenset[str]
    use_context: bool
    use_verifier: bool = False

    def select(self, agents: list[ReviewAgent]) -> list[ReviewAgent]:
        return [a for a in agents if a.role in self.roles]


_BASE = frozenset({"baseline"})
_SPEC = frozenset({"specialist"})

ARMS: dict[str, Arm] = {
    "A": Arm("A", "baseline only, no context", _BASE, False),
    "B": Arm("B", "baseline only, with context", _BASE, True),
    "C": Arm("C", "specialists only, no context", _SPEC, False),
    "D": Arm("D", "specialists only, with context", _SPEC, True),
    "E": Arm("E", "specialists + verifier, with ctx", _SPEC, True, use_verifier=True),
    # The product-shaped arm: one generalist, context, verifier. Added after
    # the corrected run showed the generalist alone (B) matching the specialists
    # (D) at under half the cost -- the question became F vs B, not E vs D.
    "F": Arm("F", "baseline + verifier, with ctx", _BASE, True, use_verifier=True),
}


# ----------------------------------------------------------------- data prep

def make_diff(case: Case) -> str:
    """Unified diff before->after, generated so hunk headers are always right."""
    chunks: list[str] = []
    for path in sorted(set(case.before) | set(case.after)):
        b = case.before.get(path, "").splitlines()
        a = case.after.get(path, "").splitlines()
        if b == a:
            continue
        fromfile = f"a/{path}" if path in case.before else "/dev/null"
        tofile = f"b/{path}" if path in case.after else "/dev/null"
        lines = list(difflib.unified_diff(b, a, fromfile=fromfile, tofile=tofile, lineterm=""))
        chunks.append("\n".join(lines))
    return "\n".join(chunks) + "\n"


def _archive_cache_dir() -> Path:
    import os

    override = os.environ.get("REVIEW_AGENT_CACHE")
    base = Path(override) if override else Path.home() / ".cache" / "review-agent"
    return base / "archives"


def _git_archive(repo: str, ref: str) -> bytes:
    """`git archive` bytes for (repo, ref), cached on disk: every arm re-materializes every case."""
    import hashlib
    import subprocess

    key = hashlib.sha1(f"{Path(repo).resolve()}@{ref}".encode("utf-8")).hexdigest()
    cache = _archive_cache_dir() / f"{key}.tar"
    if cache.exists():
        return cache.read_bytes()
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", ref], check=True, capture_output=True).stdout
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    except OSError:
        pass  # cache is an optimisation, never a requirement
    return data


def materialize(case: Case, root: Path) -> Path:
    """Write the after-state tree under root/<case.id> and return it.

    Mined cases extract the whole repository at `git_ref` (via `git archive`,
    no worktree juggling) so the context engine sees callers in files the
    diff never touched. Synthetic cases just write their after-files.
    """
    case_root = root / case.id
    case_root.mkdir(parents=True, exist_ok=True)
    if case.git_repo and case.git_ref:
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(_git_archive(case.git_repo, case.git_ref))) as tar:
            tar.extractall(case_root)
        return case_root
    for rel, text in case.after.items():
        p = case_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return case_root


# ------------------------------------------------------------------ scoring

@dataclass
class CaseScore:
    case_id: str
    clean: bool
    truths: int
    hit_strict: int
    hit_loose: int
    findings: int
    tp: int   # findings that land on some truth (loose)
    fp: int
    dup: int  # extra findings on truths that already had one
    unstructured: int
    agent_ms: int
    dropped_tp: int = 0  # verifier removed a finding that was on a real defect (its own false positive)
    dropped_fp: int = 0  # verifier removed noise (what it is for)
    tokens: int = 0      # prompt + completion across agents and verifiers, 0 for backends that don't report
    errors: list[str] = field(default_factory=list)


def score_case(findings: list[Finding], case: Case, line_tol: int = 3, agent_ms: int = 0,
               errors: list[str] | None = None, dropped: list[Finding] | None = None,
               tokens: int = 0) -> CaseScore:
    scored = [f for f in findings if not f.unstructured]

    def near(f: Finding, t) -> bool:
        return f.file == t.file and abs(f.line - t.line) <= line_tol

    hit_strict = sum(1 for t in case.truths if any(near(f, t) and f.category == t.category for f in scored))
    hit_loose = sum(1 for t in case.truths if any(near(f, t) for f in scored))
    tp = sum(1 for f in scored if any(near(f, t) for t in case.truths))
    dup = sum(max(0, sum(1 for f in scored if near(f, t)) - 1) for t in case.truths)
    dropped_scored = [f for f in (dropped or []) if not f.unstructured]
    dropped_tp = sum(1 for f in dropped_scored if any(near(f, t) for t in case.truths))
    return CaseScore(
        case_id=case.id, clean=case.clean, truths=len(case.truths),
        hit_strict=hit_strict, hit_loose=hit_loose, findings=len(scored),
        tp=tp, fp=len(scored) - tp, dup=dup, unstructured=len(findings) - len(scored),
        agent_ms=agent_ms, dropped_tp=dropped_tp, dropped_fp=len(dropped_scored) - dropped_tp,
        tokens=tokens, errors=errors or [],
    )


@dataclass
class ArmResult:
    arm: Arm
    scores: list[CaseScore]
    pool: list[dict]  # every scored finding with provenance, for label export
    ci_recall_strict: tuple[float, float] = (0.0, 0.0)
    ci_precision: tuple[float, float] = (0.0, 0.0)

    @property
    def truths(self) -> int:
        return sum(s.truths for s in self.scores)

    @property
    def recall_strict(self) -> float:
        return _ratio(sum(s.hit_strict for s in self.scores), self.truths)

    @property
    def recall_loose(self) -> float:
        return _ratio(sum(s.hit_loose for s in self.scores), self.truths)

    @property
    def findings(self) -> int:
        return sum(s.findings for s in self.scores)

    @property
    def precision(self) -> float:
        return _ratio(sum(s.tp for s in self.scores), self.findings)

    @property
    def duplicates(self) -> int:
        return sum(s.dup for s in self.scores)

    @property
    def unstructured(self) -> int:
        return sum(s.unstructured for s in self.scores)

    @property
    def fp_on_clean(self) -> int:
        return sum(s.fp for s in self.scores if s.clean)

    @property
    def clean_cases(self) -> int:
        return sum(1 for s in self.scores if s.clean)

    @property
    def agent_ms(self) -> int:
        return sum(s.agent_ms for s in self.scores)

    @property
    def agent_errors(self) -> int:
        return sum(len(s.errors) for s in self.scores)

    @property
    def dropped_tp(self) -> int:
        return sum(s.dropped_tp for s in self.scores)

    @property
    def dropped_fp(self) -> int:
        return sum(s.dropped_fp for s in self.scores)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.scores)


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def bootstrap_ci(scores: list[CaseScore], stat: Callable[[list[CaseScore]], float],
                 n: int = 1000, seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap over cases. Honest about small N: wide intervals are the point."""
    if not scores:
        return (0.0, 0.0)
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        resample = [scores[rng.randrange(len(scores))] for _ in scores]
        samples.append(stat(resample))
    samples.sort()
    lo = samples[int((alpha / 2) * n)]
    hi = samples[min(n - 1, int((1 - alpha / 2) * n))]
    return (lo, hi)


def _recall_strict(scores: list[CaseScore]) -> float:
    return _ratio(sum(s.hit_strict for s in scores), sum(s.truths for s in scores))


def _precision(scores: list[CaseScore]) -> float:
    return _ratio(sum(s.tp for s in scores), sum(s.findings for s in scores))


# ------------------------------------------------------------------ running

async def _run_case(arm: Arm, case: Case, selected: list[ReviewAgent], verifiers, root: Path,
                    line_tol: int) -> tuple[CaseScore, list[dict]]:
    case_root = materialize(case, root / arm.key)
    # The benchmark measures what the verifier would REMOVE, so it always
    # runs in drop mode regardless of the product default (flag).
    report = await run_review(make_diff(case), case_root, selected, use_context_engine=arm.use_context,
                              verifiers=verifiers if arm.use_verifier else None, verifier_mode="drop")
    agent_ms = sum(r.elapsed_ms for r in report.agent_results) + sum(v.elapsed_ms for v in report.verifier_results)
    tokens = sum(r.tokens for r in report.agent_results) + sum(v.tokens for v in report.verifier_results)
    errors = [f"{r.agent_id}: {r.error}" for r in report.agent_results if r.error]
    errors += [f"verifier {v.verifier_id}: {v.error}" for v in report.verifier_results if v.error]
    score = score_case(report.findings, case, line_tol, agent_ms, errors, dropped=report.dropped, tokens=tokens)
    pool: list[dict] = []
    for f in report.findings:
        if f.unstructured:
            continue
        is_tp = any(f.file == t.file and abs(f.line - t.line) <= line_tol for t in case.truths)
        pool.append({"arm": arm.key, "case_id": case.id, "is_tp": is_tp, **f.model_dump(mode="json")})
    return score, pool


async def run_arm(arm: Arm, cases: list[Case], agents: list[ReviewAgent], root: Path,
                  line_tol: int, verifiers: list[Verifier] | None = None, concurrency: int = 4) -> ArmResult:
    selected = arm.select(agents)
    if not selected:
        roles = sorted({a.role for a in agents})
        raise ValueError(
            f"arm {arm.key} ({arm.description}) selects no agents: needs role in "
            f"{sorted(arm.roles)}, roster has roles {roles}. Declare roles in the config."
        )
    if arm.use_verifier and not verifiers:
        raise ValueError(f"arm {arm.key} ({arm.description}) needs a verifier: declare an agent with role: verifier.")

    # Cases are independent (own checkout, own calls): run them concurrently,
    # bounded so a 60-case set does not open 60 model connections at once.
    gate = asyncio.Semaphore(max(1, concurrency))

    async def guarded(case: Case):
        async with gate:
            return await _run_case(arm, case, selected, verifiers, root, line_tol)

    outcomes = await asyncio.gather(*(guarded(c) for c in cases))
    scores = [s for s, _ in outcomes]
    pool = [row for _, rows in outcomes for row in rows]
    return ArmResult(arm=arm, scores=scores, pool=pool)


def run_bench(cases: list[Case], agents: list[ReviewAgent], arm_keys: list[str],
              line_tol: int = 3, seed: int = 0, n_boot: int = 1000,
              verifiers: list[Verifier] | None = None, concurrency: int = 4) -> list[ArmResult]:
    arms = [ARMS[k] for k in arm_keys]
    root = Path(tempfile.mkdtemp(prefix="review-agent-bench-"))
    try:
        async def _all():
            return [await run_arm(arm, cases, agents, root, line_tol, verifiers, concurrency) for arm in arms]
        results = asyncio.run(_all())
    finally:
        shutil.rmtree(root, ignore_errors=True)
    for r in results:
        r.ci_recall_strict = bootstrap_ci(r.scores, _recall_strict, n=n_boot, seed=seed)
        r.ci_precision = bootstrap_ci(r.scores, _precision, n=n_boot, seed=seed)
    return results


# ------------------------------------------------------------- repeats

REPEAT_METRICS = ("recall_strict", "recall_loose", "precision", "findings", "duplicates", "fp_on_clean",
                  "dropped_tp", "dropped_fp", "tokens", "agent_ms", "agent_errors")


def run_bench_repeated(cases: list[Case], agents: list[ReviewAgent], arm_keys: list[str], repeats: int = 1,
                       **kwargs) -> list[list[ArmResult]]:
    """The same benchmark `repeats` times. Models are not deterministic; one run is an anecdote."""
    return [run_bench(cases, agents, arm_keys, seed=i, **kwargs) for i in range(max(1, repeats))]


def summarize_repeats(runs: list[list[ArmResult]]) -> dict[str, dict[str, tuple[float, float, float]]]:
    """arm -> metric -> (mean, min, max) across runs."""
    out: dict[str, dict[str, tuple[float, float, float]]] = {}
    for arm_idx, first in enumerate(runs[0]):
        key = first.arm.key
        out[key] = {}
        for m in REPEAT_METRICS:
            vals = [float(getattr(run[arm_idx], m)) for run in runs]
            out[key][m] = (sum(vals) / len(vals), min(vals), max(vals))
    return out


def format_repeats_table(runs: list[list[ArmResult]], label: str = "") -> str:
    s = summarize_repeats(runs)
    n = len(runs)

    def cell(arm: str, m: str, fmt: str) -> str:
        mean, lo, hi = s[arm][m]
        return (fmt % mean) if n == 1 else f"{fmt % mean} [{fmt % lo},{fmt % hi}]"

    head = (f"{'arm':<4}{'description':<34}{'recall(strict)':<20}{'precision':<20}{'FP/clean':<16}"
            f"{'find':<16}{'dup':<12}{'tokens':<20}{'err'}")
    rows = [(f"{label}  " if label else "") + f"runs: {n}", head, "-" * len(head)]
    for arm_result in runs[0]:
        k = arm_result.arm.key
        rows.append(f"{k:<4}{arm_result.arm.description:<34}{cell(k, 'recall_strict', '%.2f'):<20}"
                    f"{cell(k, 'precision', '%.2f'):<20}{cell(k, 'fp_on_clean', '%.0f'):<16}{cell(k, 'findings', '%.0f'):<16}"
                    f"{cell(k, 'duplicates', '%.0f'):<12}{cell(k, 'tokens', '%.0f'):<20}{cell(k, 'agent_errors', '%.0f')}")
    return "\n".join(rows)


def repeats_json(runs: list[list[ArmResult]]) -> dict:
    return {"repeats": [results_json(run) for run in runs], "summary": summarize_repeats(runs)}


# -------------------------------------------------------------- reports

def load_results(path: Path) -> dict:
    """Read a `bench --json` archive (single run or repeats) into a uniform shape."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    # A repeats archive carries the runs as a list; a single-run archive may
    # carry `repeats: 1` as plain metadata.
    if isinstance(data.get("repeats"), list):
        return {"label": Path(path).name, "runs": data["repeats"], "summary": data["summary"], "meta": data.get("meta", {})}
    arms = {k: v for k, v in data.items() if k in ARMS}
    runs = [arms]
    summary = {k: {m: (float(v.get(m, 0)), float(v.get(m, 0)), float(v.get(m, 0))) for m in REPEAT_METRICS} for k, v in arms.items()}
    return {"label": Path(path).name, "runs": runs, "summary": summary,
            "meta": {k: v for k, v in data.items() if k not in ARMS}}


def format_report(loaded: list[dict]) -> str:
    """One table across archives: rows are (archive, arm); repeated archives show mean [min,max]."""
    head = (f"{'archive':<44}{'arm':<4}{'recall(strict)':<20}{'precision':<20}{'FP/clean':<12}{'find':<14}"
            f"{'dup':<10}{'drop tp/fp':<12}{'tokens':<20}{'runs'}")
    rows = [head, "-" * len(head)]
    for item in loaded:
        n = len(item["runs"])
        for arm in sorted(item["summary"]):
            s = item["summary"][arm]

            def c(m: str, fmt: str) -> str:
                mean, lo, hi = s[m]
                return (fmt % mean) if n == 1 else f"{fmt % mean} [{fmt % lo},{fmt % hi}]"

            drop = f"{c('dropped_tp', '%.0f')}/{c('dropped_fp', '%.0f')}" if arm in ("E", "F") else "-"
            rows.append(f"{item['label'][:43]:<44}{arm:<4}{c('recall_strict', '%.2f'):<20}{c('precision', '%.2f'):<20}"
                        f"{c('fp_on_clean', '%.0f'):<12}{c('findings', '%.0f'):<14}{c('duplicates', '%.0f'):<10}{drop:<12}"
                        f"{c('tokens', '%.0f'):<20}{n}")
    return "\n".join(rows)


# ------------------------------------------------------- label import

@dataclass
class LabelReport:
    by_arm: dict[str, dict[str, int]] = field(default_factory=dict)   # arm -> counts
    disagreements: list[dict] = field(default_factory=list)
    new_truths: int = 0

    def human_precision(self, arm: str) -> float:
        c = self.by_arm.get(arm, {})
        return _ratio(c.get("yes", 0), c.get("yes", 0) + c.get("no", 0))


_YES = {"yes", "y", "1", "true", "tp", "useful", "real"}
_NO = {"no", "n", "0", "false", "fp", "noise", "wrong"}
_DUP = {"dup", "duplicate", "d"}


def read_answers(path: Path) -> dict[str, str]:
    """CSV `label_id,answer` (header optional). answer: yes | no | dup."""
    import csv

    answers: dict[str, str] = {}
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2 or row[0].strip().lower() in ("label_id", "id", ""):
                continue
            a = row[1].strip().lower()
            kind = "yes" if a in _YES else "no" if a in _NO else "dup" if a in _DUP else None
            if kind is None:
                raise ValueError(f"{path}: unrecognised answer {row[1]!r} for {row[0]} (use yes/no/dup)")
            answers[row[0].strip()] = kind
    return answers


def import_labels(pool: list[dict], answers: dict[str, str], cases: list[Case] | None = None,
                  line_tol: int = 3) -> tuple[LabelReport, list[Case]]:
    """Fold human answers into the pool: per-arm human precision, machine/human
    disagreements, and -- when cases are given -- new truths on git-backed cases
    for every `yes` not already covered. Returns the report and updated cases."""
    report = LabelReport()
    for r in pool:
        kind = answers.get(r["label_id"])
        if kind is None:
            continue
        counts = report.by_arm.setdefault(r["arm"], {"yes": 0, "no": 0, "dup": 0, "labeled": 0})
        counts[kind] += 1
        counts["labeled"] += 1
        if kind == "yes" and not r.get("is_tp"):
            report.disagreements.append({"kind": "bug by another name", **_brief(r)})
        elif kind == "no" and r.get("is_tp"):
            report.disagreements.append({"kind": "right line, wrong claim", **_brief(r)})

    updated: list[Case] = list(cases or [])
    if cases:
        by_id = {c.id: i for i, c in enumerate(updated)}
        for r in pool:
            if answers.get(r["label_id"]) != "yes" or r["case_id"] not in by_id:
                continue
            idx = by_id[r["case_id"]]
            case = updated[idx]
            if r["file"] not in case.after:
                continue
            if any(t.file == r["file"] and abs(t.line - int(r["line"])) <= line_tol for t in case.truths):
                continue
            lines = case.after[r["file"]].splitlines()
            ln = int(r["line"])
            if not (1 <= ln <= len(lines)) or not lines[ln - 1].strip():
                continue
            from review_agent.bench.cases import Truth
            from review_agent.schemas import Category

            truth = Truth(r["file"], ln, Category(r["category"]), lines[ln - 1].strip(),
                          note=f"human label {r['label_id']}: {str(r.get('message', ''))[:80]}")
            updated[idx] = Case(id=case.id, before=case.before, after=case.after, truths=case.truths + (truth,),
                                git_repo=case.git_repo, git_ref=case.git_ref, note=case.note)
            report.new_truths += 1
    return report, updated


def _brief(r: dict) -> dict:
    return {"label_id": r["label_id"], "arm": r["arm"], "case_id": r["case_id"], "file": r["file"],
            "line": r["line"], "message": str(r.get("message", ""))[:100]}


def format_label_report(rep: LabelReport) -> str:
    lines = [f"{'arm':<5}{'labeled':<9}{'yes':<5}{'no':<5}{'dup':<5}{'human precision'}"]
    for arm in sorted(rep.by_arm):
        c = rep.by_arm[arm]
        lines.append(f"{arm:<5}{c['labeled']:<9}{c['yes']:<5}{c['no']:<5}{c['dup']:<5}{rep.human_precision(arm):.2f}")
    if rep.new_truths:
        lines.append(f"\nnew truths added from 'yes' answers: {rep.new_truths}")
    if rep.disagreements:
        lines.append("\nmachine vs human:")
        for d in rep.disagreements:
            lines.append(f"  [{d['kind']}] {d['arm']} {d['case_id']} {d['file']}:{d['line']}: {d['message']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ output

def format_table(results: list[ArmResult]) -> str:
    head = (f"{'arm':<4}{'description':<34}{'recall(strict)':<20}{'loose':<7}{'precision':<20}"
            f"{'FP/clean':<9}{'find':<6}{'dup':<5}{'drop tp/fp':<12}{'unstr':<7}{'agent_ms':<10}{'tokens':<9}{'errors'}")
    rows = [head, "-" * len(head)]
    for r in results:
        rs = f"{r.recall_strict:.2f} [{r.ci_recall_strict[0]:.2f},{r.ci_recall_strict[1]:.2f}]"
        pr = f"{r.precision:.2f} [{r.ci_precision[0]:.2f},{r.ci_precision[1]:.2f}]"
        fpc = f"{r.fp_on_clean}/{r.clean_cases}"
        drop = f"{r.dropped_tp}/{r.dropped_fp}" if r.arm.use_verifier else "-"
        rows.append(f"{r.arm.key:<4}{r.arm.description:<34}{rs:<20}{r.recall_loose:<7.2f}{pr:<20}"
                    f"{fpc:<9}{r.findings:<6}{r.duplicates:<5}{drop:<12}{r.unstructured:<7}{r.agent_ms:<10}{r.tokens:<9}{r.agent_errors}")
    return "\n".join(rows)


def results_json(results: list[ArmResult]) -> dict:
    return {
        r.arm.key: {
            "description": r.arm.description,
            "truths": r.truths,
            "recall_strict": r.recall_strict, "recall_strict_ci": r.ci_recall_strict,
            "recall_loose": r.recall_loose,
            "precision": r.precision, "precision_ci": r.ci_precision,
            "fp_on_clean": r.fp_on_clean, "clean_cases": r.clean_cases,
            "findings": r.findings, "duplicates": r.duplicates, "unstructured": r.unstructured,
            "dropped_tp": r.dropped_tp, "dropped_fp": r.dropped_fp,
            "agent_ms": r.agent_ms, "tokens": r.tokens, "agent_errors": r.agent_errors,
            "cases": [s.__dict__ for s in r.scores],
        }
        for r in results
    }


# ------------------------------------------------------- verifier evaluation

@dataclass
class VerifierEval:
    """Replay a labeled candidate pool through a verifier.

    End-to-end arms are the wrong instrument for a precision stage: on a
    near-noise-free dataset the verifier is handed nothing to remove and
    scores 0/0 whatever it would have done. A pool of real first-stage
    output with ground-truth `is_tp` labels asks the question directly.

    Scored PER TRUTH, not per candidate. The first version counted every
    dropped true positive as a kill and reported 30% -- but it had merged
    two runs into the same groups, so the verifier was correctly removing
    the same finding seen twice. A truth is *lost* only when every
    candidate covering it is dropped. A dropped TP whose truth is still
    covered is successful dedup: the 19-comments-for-14-defects noise the
    PRD complained about, removed.

    Groups are keyed by (pool, arm, case): candidates from different runs
    never share a group, because they never shared a review.
    """
    groups: int = 0
    candidates: int = 0
    tp: int = 0
    fp: int = 0
    dropped_tp: int = 0
    dropped_fp: int = 0
    truths_covered: int = 0   # truths with >=1 candidate before the verifier
    truths_lost: int = 0      # truths with 0 candidates after it -- the verifier's real false positives
    dup_removed: int = 0      # dropped TPs whose truth stayed covered -- dedup that worked
    errors: int = 0
    elapsed_ms: int = 0
    drops: list[dict] = field(default_factory=list)   # every drop, with reason and kind: fp | dup | lost

    @property
    def precision_before(self) -> float:
        return _ratio(self.tp, self.candidates)

    @property
    def precision_after(self) -> float:
        kept = self.candidates - self.dropped_tp - self.dropped_fp
        return _ratio(self.tp - self.dropped_tp, kept)

    @property
    def fp_drop_rate(self) -> float:
        return _ratio(self.dropped_fp, self.fp)

    @property
    def truth_loss_rate(self) -> float:
        return _ratio(self.truths_lost, self.truths_covered)


def load_pool(labels_path: Path) -> list[dict]:
    """Join a labels JSONL with its .key.json back into full rows, tagged with the pool name."""
    key_path = labels_path.with_suffix(labels_path.suffix + ".key.json")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    rows = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows.append({**r, **key[r["label_id"]], "pool": labels_path.name})
    return rows


async def run_verifier_eval(pool: list[dict], cases: list[Case], verifier: Verifier, root: Path,
                            use_context: bool = True, line_tol: int = 3) -> VerifierEval:
    from collections import defaultdict

    from review_agent.context.engine import ContextEngine
    from review_agent.diff import parse_diff
    from review_agent.agents.verifier import apply_verdicts
    from review_agent.schemas import Category, ContextBundle, Severity

    cases_by_id = {c.id: c for c in cases}
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in pool:
        groups[(r.get("pool", ""), r["arm"], r["case_id"])].append(r)

    ev = VerifierEval()
    for (pool_name, arm, cid), rows in sorted(groups.items()):
        case = cases_by_id[cid]
        case_root = materialize(case, root / arm)
        hunks = parse_diff(make_diff(case))
        context = ContextEngine(case_root).build(hunks) if use_context else ContextBundle(hunks=hunks)
        findings = [
            Finding(file=r["file"], line=int(r["line"]), severity=Severity(r["severity"]),
                    category=Category(r["category"]), message=r["message"], reasoning=r.get("reasoning", ""),
                    source_agent=r["source_agent"], merged_from=list(r.get("merged_from", [])))
            for r in rows
        ]
        result = await verifier.verify(findings, context)
        kept, dropped = apply_verdicts(findings, [result])
        # apply_verdicts keeps original objects for kept and returns copies for dropped, in order.
        dropped_idx = [i for i, f in enumerate(findings) if not any(k is f for k in kept)]
        reason_by_idx = dict(zip(dropped_idx, (d.dropped_reason for d in dropped)))

        def near(f: Finding, t) -> bool:
            return f.file == t.file and abs(f.line - t.line) <= line_tol

        covered_before = {ti for ti, t in enumerate(case.truths) if any(near(f, t) for f in findings)}
        covered_after = {ti for ti, t in enumerate(case.truths) if any(near(f, t) for f in kept)}

        ev.groups += 1
        ev.candidates += len(rows)
        ev.tp += sum(1 for r in rows if r["is_tp"])
        ev.fp += sum(1 for r in rows if not r["is_tp"])
        ev.errors += 1 if result.error else 0
        ev.elapsed_ms += result.elapsed_ms
        ev.truths_covered += len(covered_before)
        ev.truths_lost += len(covered_before - covered_after)

        for i in dropped_idx:
            r, f = rows[i], findings[i]
            if not r["is_tp"]:
                kind = "fp"
                ev.dropped_fp += 1
            else:
                ev.dropped_tp += 1
                my_truths = {ti for ti, t in enumerate(case.truths) if near(f, t)}
                kind = "dup" if my_truths <= covered_after else "lost"
                if kind == "dup":
                    ev.dup_removed += 1
            ev.drops.append({"pool": pool_name, "arm": arm, "case_id": cid, "source_agent": r["source_agent"],
                             "is_tp": r["is_tp"], "kind": kind, "line": f.line,
                             "message": f.message[:120], "reason": reason_by_idx[i][:200]})
    return ev


def run_verifier_eval_sync(pool: list[dict], cases: list[Case], verifier: Verifier,
                           use_context: bool = True, line_tol: int = 3) -> VerifierEval:
    root = Path(tempfile.mkdtemp(prefix="review-agent-vbench-"))
    try:
        return asyncio.run(run_verifier_eval(pool, cases, verifier, root, use_context, line_tol))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def format_verifier_eval(ev: VerifierEval) -> str:
    lines = [
        f"groups: {ev.groups}   candidates: {ev.candidates} (tp={ev.tp}, fp={ev.fp})   errors: {ev.errors}   verifier time: {ev.elapsed_ms // 1000}s",
        f"false positives dropped:   {ev.dropped_fp} of {ev.fp} ({ev.fp_drop_rate:.0%})",
        f"duplicate TPs removed:     {ev.dup_removed}  (truth still covered -- dedup that worked)",
        f"truths LOST:               {ev.truths_lost} of {ev.truths_covered} covered ({ev.truth_loss_rate:.0%})  <- the verifier's own false positives",
        f"precision before: {ev.precision_before:.2f}   after: {ev.precision_after:.2f}",
    ]
    order = {"lost": 0, "fp": 1, "dup": 2}
    shown = [d for d in ev.drops if d["kind"] != "dup"]
    if shown:
        lines.append("")
        for d in sorted(shown, key=lambda d: order[d["kind"]]):
            tag = {"lost": "TRUTH LOST", "fp": "fp dropped"}[d["kind"]]
            lines.append(f"  [{tag}] {d['pool']}/{d['arm']} {d['case_id']} L{d['line']} ({d['source_agent']}): {d['message']}")
            lines.append(f"      -> {d['reason']}")
    dups = [d for d in ev.drops if d["kind"] == "dup"]
    if dups:
        lines.append(f"\n  ({len(dups)} duplicate removals not listed; see --json for all)")
    return "\n".join(lines)


def export_labels(results: list[ArmResult], out: Path, seed: int = 0) -> tuple[Path, Path]:
    """Write a shuffled, provenance-stripped JSONL for blind labeling plus a key.

    Labelers see file/line/severity/message/reasoning only. The key maps
    label_id back to arm, source agents and ground-truth hit so human labels
    can be re-attributed afterwards without the labeler ever knowing.
    """
    rng = random.Random(seed)
    pool = [dict(p) for r in results for p in r.pool]
    rng.shuffle(pool)
    key_path = out.with_suffix(out.suffix + ".key.json")
    key: dict[str, dict] = {}
    with out.open("w", encoding="utf-8") as fh:
        for i, p in enumerate(pool):
            label_id = f"L{i:04d}"
            key[label_id] = {"arm": p["arm"], "case_id": p["case_id"], "source_agent": p["source_agent"],
                             "merged_from": p["merged_from"], "is_tp": p["is_tp"], "category": p["category"]}
            visible = {"label_id": label_id, "file": p["file"], "line": p["line"], "severity": p["severity"],
                       "message": p["message"], "reasoning": p["reasoning"]}
            fh.write(json.dumps(visible, ensure_ascii=False) + "\n")
    key_path.write_text(json.dumps(key, indent=2), encoding="utf-8")
    return out, key_path
