import json
from pathlib import Path

import pytest

from review_agent.agents.fake_agent import FakeAgent
from review_agent.agents.heuristic_agent import HeuristicAgent
from review_agent.bench.cases import CASES, Case, Truth, load_cases
from review_agent.bench.runner import (
    bootstrap_ci,
    export_labels,
    make_diff,
    materialize,
    run_bench,
    score_case,
)
from review_agent.diff import parse_diff
from review_agent.schemas import Category, Finding, Severity


# ------------------------------------------------------------ dataset integrity

def test_dataset_loads_and_has_both_kinds():
    cases = load_cases()
    assert len(cases) >= 20
    assert sum(1 for c in cases if c.clean) >= 6
    assert sum(len(c.truths) for c in cases) >= 14


def test_loader_rejects_misplaced_snippet():
    from review_agent.bench.cases import validate_cases

    bad = Case(id="x", before={"f.py": "a\n"}, after={"f.py": "a\nb\n"},
               truths=(Truth("f.py", 1, Category.LOGIC, "b"),))
    with pytest.raises(ValueError, match="snippet 'b' not on f.py:1"):
        validate_cases([bad])
    with pytest.raises(ValueError, match="duplicate case ids"):
        validate_cases([CASES[0], CASES[0]])


def test_suites_load_and_crossfile_has_mirrors():
    cross = load_cases("crossfile")
    assert len(cross) == 12
    assert sum(1 for c in cross if c.clean) == 6
    # every defect case has a clean mirror sharing the diff file's after-state
    for c in cross:
        if c.clean:
            continue
        diff_files = [p for p in c.after if c.before.get(p) != c.after[p]]
        assert len(diff_files) == 1, f"{c.id} should change exactly one file"
        twin = next(m for m in cross if m.clean and m.after.get(diff_files[0]) == c.after[diff_files[0]])
        assert twin.id.startswith("xf-clean-")
    assert len(load_cases("all")) == len(load_cases("builtin")) + 12
    with pytest.raises(ValueError, match="unknown suite"):
        load_cases("nope")


def test_cases_round_trip_through_json(tmp_path):
    from review_agent.bench.cases import read_cases, write_cases

    cases = load_cases("crossfile")[:3]
    write_cases(cases, tmp_path / "c.json")
    back = read_cases(tmp_path / "c.json")
    assert [c.id for c in back] == [c.id for c in cases]
    assert back[0].truths == cases[0].truths and back[0].after == cases[0].after


from review_agent.bench.cases import CROSSFILE_CASES  # noqa: E402


@pytest.mark.parametrize("case", CASES + CROSSFILE_CASES, ids=[c.id for c in CASES + CROSSFILE_CASES])
def test_generated_diff_parses_and_covers_every_truth(case):
    hunks = parse_diff(make_diff(case))
    assert hunks, f"{case.id}: diff produced no hunks"
    for t in case.truths:
        spans = [(h.start_line, h.end_line) for h in hunks if h.file == t.file]
        assert any(lo - 3 <= t.line <= hi + 3 for lo, hi in spans), f"{case.id}: truth {t.file}:{t.line} not inside any hunk {spans}"


def test_diff_carries_added_line_numbers():
    case = next(c for c in CASES if c.id == "sec-sql-injection")
    h = parse_diff(make_diff(case))[0]
    assert h.added_line_numbers == [2]
    assert 'f"SELECT' in h.added_lines[0]


def test_new_file_case_diffs_as_added_file():
    case = next(c for c in CASES if c.id == "clean-add-test-file")
    hunks = parse_diff(make_diff(case))
    assert [h.file for h in hunks] == ["tests/test_mathx.py"]


def test_materialize_writes_after_tree(tmp_path: Path):
    case = next(c for c in CASES if c.id == "clean-add-test-file")
    root = materialize(case, tmp_path)
    assert (root / "app" / "mathx.py").exists()
    assert (root / "tests" / "test_mathx.py").read_text().startswith("from app.mathx")


# ------------------------------------------------------------------- scoring

def _f(file, line, cat=Category.SECURITY):
    return Finding(file=file, line=line, severity=Severity.WARNING, category=cat, message="m")


def test_score_strict_vs_loose_and_fp():
    case = Case(id="c", before={}, after={"f.py": "x\n" * 30},
                truths=(Truth("f.py", 10, Category.SECURITY, "x"), Truth("f.py", 25, Category.LOGIC, "x")))
    findings = [
        _f("f.py", 12, Category.SECURITY),   # strict hit on truth 1 (within 3)
        _f("f.py", 26, Category.SECURITY),   # loose-only hit on truth 2 (wrong category)
        _f("f.py", 1, Category.SECURITY),    # false positive
        _f("g.py", 10, Category.SECURITY),   # wrong file -> fp
    ]
    s = score_case(findings, case, line_tol=3)
    assert (s.truths, s.hit_strict, s.hit_loose) == (2, 1, 2)
    assert (s.findings, s.tp, s.fp) == (4, 2, 2)


def test_score_clean_case_all_findings_are_fp():
    case = Case(id="c", before={}, after={"f.py": "x\n"})
    s = score_case([_f("f.py", 1)], case)
    assert s.clean and s.fp == 1 and s.tp == 0


def test_bootstrap_ci_is_bounded_and_deterministic():
    case = Case(id="c", before={}, after={"f.py": "x\n" * 5}, truths=(Truth("f.py", 1, Category.LOGIC, "x"),))
    scores = [score_case([_f("f.py", 1, Category.LOGIC)], case), score_case([], case), score_case([], case)]
    stat = lambda ss: sum(s.hit_strict for s in ss) / max(1, sum(s.truths for s in ss))
    lo, hi = bootstrap_ci(scores, stat, n=200, seed=1)
    assert 0.0 <= lo <= 1 / 3 <= hi <= 1.0
    assert bootstrap_ci(scores, stat, n=200, seed=1) == (lo, hi)


# --------------------------------------------------------------- end to end

def test_run_bench_heuristic_beats_noise_floor():
    cases = load_cases()
    heuristic = run_bench(cases, [HeuristicAgent(role="baseline")], ["A", "B"], n_boot=50)
    noise = run_bench(cases, [FakeAgent("noise", Category.LOGIC, role="baseline")], ["B"], n_boot=50)

    a, b = heuristic
    assert a.recall_strict >= 0.5
    assert a.fp_on_clean == 0
    assert a.agent_errors == 0
    assert a.duplicates == 0
    # heuristic never reads the context bundle, so B must equal A exactly
    assert (b.recall_strict, b.precision, b.findings) == (a.recall_strict, a.precision, a.findings)
    assert noise[0].precision < a.precision


def test_arms_select_by_role_not_position():
    cases = load_cases()
    roster = [HeuristicAgent(role="baseline"), FakeAgent("noise", Category.LOGIC)]  # fake = specialist
    a, c, d = run_bench(cases, roster, ["A", "C", "D"], n_boot=20)
    # A = heuristic alone; C/D = the fake specialist alone, never the heuristic.
    assert a.recall_strict >= 0.5 and a.fp_on_clean == 0
    # FakeAgent only reads context.symbols: without the context engine it is silent.
    assert c.findings == 0
    # With context it speaks, and the noise lands where it should.
    assert d.findings > 0 and d.fp_on_clean > 0
    assert d.precision < a.precision


def test_arm_with_no_agents_of_required_role_raises():
    cases = load_cases()[:2]
    with pytest.raises(ValueError, match=r"arm A .* selects no agents"):
        run_bench(cases, [HeuristicAgent()], ["A"], n_boot=5)        # specialist only, no baseline
    with pytest.raises(ValueError, match=r"arm C .* selects no agents"):
        run_bench(cases, [HeuristicAgent(role="baseline")], ["C"], n_boot=5)


def test_dup_metric_counts_extra_findings_on_hit_truths():
    case = Case(id="c", before={}, after={"f.py": "x\n" * 30},
                truths=(Truth("f.py", 10, Category.SECURITY, "x"), Truth("f.py", 25, Category.LOGIC, "x")))
    findings = [
        _f("f.py", 10, Category.SECURITY),  # truth 1, first
        _f("f.py", 11, Category.LOGIC),     # truth 1, duplicate (different label)
        _f("f.py", 9, Category.SECURITY),   # truth 1, duplicate
        _f("f.py", 25, Category.LOGIC),     # truth 2, first
    ]
    s = score_case(findings, case, line_tol=3)
    assert (s.tp, s.fp, s.dup) == (4, 0, 2)


def test_unstructured_findings_are_not_scored():
    case = Case(id="c", before={}, after={"f.py": "x\n"}, truths=(Truth("f.py", 1, Category.LOGIC, "x"),))
    raw = Finding(file="UNKNOWN", line=0, severity=Severity.INFO, category=Category.LOGIC, message="blah", unstructured=True)
    s = score_case([raw], case)
    assert (s.findings, s.fp, s.unstructured, s.hit_loose) == (0, 0, 1, 0)


def test_bench_sums_tokens_from_agents_that_report_them():
    from review_agent.agents.base import TimedAgent

    class Billed(TimedAgent):
        agent_id = "billed"
        role = "baseline"

        async def _run(self, context):
            self.last_usage = (10, 5)
            return []

    cases = load_cases()
    (a,) = run_bench(cases, [Billed()], ["A"], n_boot=5)
    assert a.tokens == 15 * len(cases)
    (h,) = run_bench(cases, [HeuristicAgent(role="baseline")], ["A"], n_boot=5)
    assert h.tokens == 0  # a backend that does not report usage counts as 0, never as missing


def test_export_labels_strips_provenance(tmp_path: Path):
    cases = load_cases()[:4]
    results = run_bench(cases, [HeuristicAgent(role="baseline")], ["A"], n_boot=10)
    out, key_path = export_labels(results, tmp_path / "labels.jsonl", seed=3)

    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert rows, "expected at least one finding to label"
    for row in rows:
        assert set(row) == {"label_id", "file", "line", "severity", "message", "reasoning"}
    key = json.loads(key_path.read_text(encoding="utf-8"))
    assert set(key) == {r["label_id"] for r in rows}
    assert all({"arm", "case_id", "source_agent", "is_tp"} <= set(v) for v in key.values())
