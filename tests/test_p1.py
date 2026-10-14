"""P1: repeats, report, compare, label import, miner quality, archive cache."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from review_agent.agents.fake_agent import FakeAgent
from review_agent.agents.heuristic_agent import HeuristicAgent
from review_agent.bench.cases import Case, Truth, load_cases, read_cases, write_cases
from review_agent.bench.mine import _is_test_path, _snap_line
from review_agent.bench.runner import (
    export_labels,
    format_report,
    import_labels,
    load_pool,
    load_results,
    read_answers,
    results_json,
    run_bench,
    run_bench_repeated,
    summarize_repeats,
)
from review_agent.cli import app
from review_agent.schemas import Category


# ------------------------------------------------------------------ repeats

def test_repeats_summary_has_mean_min_max_and_is_deterministic_for_heuristic():
    cases = load_cases()[:6]
    runs = run_bench_repeated(cases, [HeuristicAgent(role="baseline")], ["A"], repeats=3, n_boot=5)
    assert len(runs) == 3
    s = summarize_repeats(runs)
    mean, lo, hi = s["A"]["recall_strict"]
    assert lo == hi == mean  # a deterministic backend has zero spread
    assert set(s["A"]) >= {"recall_strict", "precision", "findings", "tokens", "fp_on_clean"}


def test_concurrency_does_not_change_scores():
    cases = load_cases()
    (serial,) = run_bench(cases, [HeuristicAgent(role="baseline")], ["A"], n_boot=5, concurrency=1)
    (parallel,) = run_bench(cases, [HeuristicAgent(role="baseline")], ["A"], n_boot=5, concurrency=8)

    def without_timing(score):
        return {k: v for k, v in score.__dict__.items() if k != "agent_ms"}

    assert [without_timing(s) for s in serial.scores] == [without_timing(s) for s in parallel.scores]
    assert serial.pool == parallel.pool  # order preserved: pools must stay comparable across runs


# ------------------------------------------------------------------- report

def test_report_reads_single_and_repeated_archives(tmp_path):
    cases = load_cases()[:6]
    (single,) = run_bench(cases, [HeuristicAgent(role="baseline")], ["A"], n_boot=5)
    # exactly what `bench --json` writes for one run: `repeats: 1` sits at top level as metadata
    (tmp_path / "single.json").write_text(json.dumps({"roster": "h", "source": "x", "cases": 6, "repeats": 1, **results_json([single])}), encoding="utf-8")
    runs = run_bench_repeated(cases, [HeuristicAgent(role="baseline")], ["A"], repeats=2, n_boot=5)
    from review_agent.bench.runner import repeats_json
    (tmp_path / "rep.json").write_text(json.dumps({"meta": {"roster": "h"}, **repeats_json(runs)}), encoding="utf-8")

    a, b = load_results(tmp_path / "single.json"), load_results(tmp_path / "rep.json")
    assert len(a["runs"]) == 1 and len(b["runs"]) == 2
    table = format_report([a, b])
    assert "single.json" in table and "rep.json" in table and table.count("\n") >= 3

    result = CliRunner().invoke(app, ["report", str(tmp_path / "single.json"), str(tmp_path / "rep.json")])
    assert result.exit_code == 0, result.output
    assert "recall(strict)" in result.output


# ------------------------------------------------------------------ compare

def test_compare_runs_each_config_and_prints_one_table(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text('version: "1"\nagents:\n  - id: h\n    type: heuristic\n    role: baseline\n', encoding="utf-8")
    b.write_text('version: "1"\nagents:\n  - id: noise\n    type: fake\n    role: baseline\n    category: logic\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["compare", "--config", str(a), "--config", str(b), "--arms", "B", "--repeats", "1"])
    assert result.exit_code == 0, result.output
    assert "a.yaml" in result.output and "b.yaml" in result.output
    js = CliRunner().invoke(app, ["compare", "--config", str(a), "--arms", "B", "--repeats", "2", "--json"])
    data = json.loads(js.output)
    assert data["repeats"] == 2 and str(a) in data["configs"]


# ------------------------------------------------------------- label import

def test_label_import_scores_humans_and_adds_truths(tmp_path):
    cases = load_cases()
    roster = [HeuristicAgent(role="baseline"), FakeAgent("noise", Category.LOGIC)]
    results = run_bench(cases, roster, ["B", "D"], n_boot=5)
    labels, _ = export_labels(results, tmp_path / "labels.jsonl")
    pool = load_pool(labels)

    # Humans: everything from the heuristic is useful; one noise finding on a
    # clean case is "yes" anyway (a bug by another name), the rest are "no".
    rows = ["label_id,answer"]
    yes_on_clean = None
    for r in pool:
        if r["source_agent"] == "heuristic":
            rows.append(f"{r['label_id']},yes")
        elif r["case_id"].startswith("clean-") and yes_on_clean is None:
            yes_on_clean = r
            rows.append(f"{r['label_id']},YES")
        else:
            rows.append(f"{r['label_id']},no")
    (tmp_path / "answers.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    answers = read_answers(tmp_path / "answers.csv")
    rep, updated = import_labels(pool, answers, cases)
    assert rep.human_precision("B") == 1.0
    assert rep.by_arm["D"]["no"] > 0
    kinds = {d["kind"] for d in rep.disagreements}
    assert "bug by another name" in kinds            # the yes on a clean case
    assert rep.new_truths >= 1
    clean_case = next(c for c in updated if c.id == yes_on_clean["case_id"])
    assert any(t.note.startswith("human label") for t in clean_case.truths)

    bad = tmp_path / "bad.csv"
    bad.write_text("L0000,maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognised answer"):
        read_answers(bad)

    write_cases(cases, tmp_path / "cases.json")
    result = CliRunner().invoke(app, ["label-import", "--pool", str(labels), "--answers", str(tmp_path / "answers.csv"),
                                      "--cases", str(tmp_path / "cases.json"), "--out", str(tmp_path / "labeled.json")])
    assert result.exit_code == 0, result.output
    assert "human precision" in result.output
    assert sum(len(c.truths) for c in read_cases(tmp_path / "labeled.json")) > sum(len(c.truths) for c in cases)


# ------------------------------------------------------------- miner quality

def test_non_bug_subjects_are_filtered():
    from review_agent.bench.mine import NOT_A_BUG

    for subject in ("Tuple test_requirements was too long, PEP8", "Document that 'params' can also be a list",
                    "Grammar fix (#6133)", "Fix `extensions` type annotation", "Bump mypy from 0.910", "fix wording"):
        assert NOT_A_BUG.search(subject), subject
    for subject in ("Fix exception suppression in ASGITransport", "Preserve header casing", "fix: NO_PROXY should support IPv6"):
        assert not NOT_A_BUG.search(subject), subject


def test_test_paths_and_trivial_lines_are_rejected_as_truths():
    assert _is_test_path("tests/models/test_cookies.py")
    assert _is_test_path("pkg/foo_test.py") and _is_test_path("conftest.py") and _is_test_path("src/__tests__/a.py")
    assert not _is_test_path("httpx/_models.py") and not _is_test_path("app/contest.py")

    lines = ["def f():", "    try:", "        x = compute()", "    except Exception:", "        pass", "    )", "    return x"]
    assert _snap_line(lines, 3) == 3          # real code stays
    assert _snap_line(lines, 2) == 1          # `try:` -> nearest real line; the def line counts (a changed signature is a truth)
    assert _snap_line(lines, 5) == 3          # `pass`: neighbours `except ...:` and `)` are trivial; distance 2, lower first
    assert _snap_line(lines, 6) == 7          # `)` -> return x
    assert _snap_line(["", "", "", ""], 2) is None
    assert _snap_line(["x = 1", "    )", "    ]"], 3, radius=1) is None  # nothing real within reach


def test_git_archive_is_cached(tmp_path, monkeypatch):
    import subprocess

    from review_agent.bench.runner import _git_archive, materialize

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-q", "-m", "i"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    monkeypatch.setenv("REVIEW_AGENT_CACHE", str(tmp_path / "cache"))
    data1 = _git_archive(str(repo), sha)
    cached = list((tmp_path / "cache" / "archives").glob("*.tar"))
    assert len(cached) == 1 and cached[0].read_bytes() == data1
    cached[0].write_bytes(b"poisoned")       # prove the second call reads the cache, not git
    assert _git_archive(str(repo), sha) == b"poisoned"
    cached[0].write_bytes(data1)
    case = Case(id="c", before={}, after={"a.py": "x = 1\n"}, git_repo=str(repo), git_ref=sha)
    assert (materialize(case, tmp_path / "m") / "a.py").read_text(encoding="utf-8") == "x = 1\n"
