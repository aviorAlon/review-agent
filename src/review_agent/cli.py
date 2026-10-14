"""CLI. Local-diff mode is the primary interface on purpose: a tool that
takes a diff and a checkout and prints JSON can be dropped into any CI as
one shell step, which is the honest version of "platform agnostic".
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import typer

# Typer renders --help with rich when it is installed; on a legacy Windows
# console with stdout piped (e.g. `review-agent --help | head`) rich's
# renderer raises OSError mid-write. Plain help when not attached to a TTY.
if not sys.stdout.isatty():
    try:
        import typer.core as _typer_core

        _typer_core.rich = None  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cosmetic; never block the CLI on this
        pass

from review_agent.agents.fake_agent import FakeAgent
from review_agent.config import build_agents, build_verifiers, load_config
from review_agent.pipeline import run_review
from review_agent.providers.base import VCSProvider
from review_agent.providers.github import GitHubProvider
from review_agent.providers.gitlab import GitLabProvider
from review_agent.schemas import Category


def _make_provider(choice: str) -> VCSProvider | None:
    """`auto` sniffs the CI host from the env vars GitLab CI / GitHub Actions always set."""
    choice = choice.lower()
    if choice == "none":
        return None
    if choice == "auto":
        if os.environ.get("GITLAB_CI"):
            choice = "gitlab"
        elif os.environ.get("GITHUB_ACTIONS"):
            choice = "github"
        else:
            raise typer.BadParameter("not running in GitLab CI or GitHub Actions; pass --post gitlab|github explicitly")
    if choice == "gitlab":
        return GitLabProvider.from_env()
    if choice == "github":
        return GitHubProvider.from_env()
    raise typer.BadParameter(f"unknown --post target '{choice}' (use none|auto|gitlab|github)")

app = typer.Typer(add_completion=False, help="Multi-agent code review enhancement layer.")


def _read_diff(diff_file: Path | None, repo: Path, base: str | None) -> str:
    if diff_file is not None:
        return diff_file.read_text(encoding="utf-8")
    ref = base or "HEAD"
    out = subprocess.run(["git", "diff", ref], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout


def has_blocking_findings(findings) -> bool:
    """The CI gate: a critical finding the verifier did not downgrade."""
    return any(f.severity.value == "critical" and not f.flagged and not f.unstructured for f in findings)


def write_audit(report, path: Path, agents, verifiers, cfg, source: str) -> Path:
    """One JSON per run: what ran, on what, what it cost, what it said and why.

    This is the record an enterprise reviewer asks for before trusting a bot:
    model per agent, tokens, every verifier verdict with its reason, what was
    excluded. Prompts themselves are not stored (they contain the code).
    """
    import datetime as _dt

    path.mkdir(parents=True, exist_ok=True)
    out = path / f"{report.run_id}.json"
    record = {
        "run_id": report.run_id,
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "config": {
            "verifier_mode": cfg.verifier_mode,
            "limits": cfg.limits.model_dump(),
            "exclude": cfg.exclude,
            "aggregation": cfg.aggregation.model_dump(),
            "agents": [{"id": a.agent_id, "role": a.role, "backend": type(a).__name__, "model": getattr(a, "model", "")} for a in agents],
            "verifiers": [{"id": v.verifier_id, "backend": type(v).__name__, "model": getattr(v, "model", "")} for v in verifiers],
        },
        "input": {"hunks": len(report.context.hunks), "files": sorted({h.file for h in report.context.hunks}),
                  "symbols_resolved": len(report.context.symbols), "unsupported_files": report.unsupported_files,
                  "notes": report.notes},
        "agents": [{"agent_id": r.agent_id, "findings": len(r.findings), "error": r.error, "elapsed_ms": r.elapsed_ms,
                    "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens} for r in report.agent_results],
        "verifiers": [{"verifier_id": v.verifier_id, "error": v.error, "elapsed_ms": v.elapsed_ms,
                       "prompt_tokens": v.prompt_tokens, "completion_tokens": v.completion_tokens,
                       "verdicts": [{"index": d.index, "keep": d.keep, "reason": d.reason} for d in v.verdicts]}
                      for v in report.verifier_results],
        "findings": [f.model_dump(mode="json") for f in report.findings],
        "dropped": [f.model_dump(mode="json") for f in report.dropped],
        "tokens": sum(r.tokens for r in report.agent_results) + sum(v.tokens for v in report.verifier_results),
    }
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _emit(report, as_json: bool) -> None:
    if as_json:
        payload = {
            "run_id": report.run_id,
            "findings": [f.model_dump(mode="json") for f in report.findings],
            "dropped": [f.model_dump(mode="json") for f in report.dropped],
            "flagged": len(report.flagged),
            "notes": report.notes,
            "agents": [
                {"agent_id": r.agent_id, "count": len(r.findings), "error": r.error, "elapsed_ms": r.elapsed_ms,
                 "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens}
                for r in report.agent_results
            ],
            "verifiers": [
                {"verifier_id": v.verifier_id, "verdicts": len(v.verdicts), "error": v.error, "elapsed_ms": v.elapsed_ms,
                 "prompt_tokens": v.prompt_tokens, "completion_tokens": v.completion_tokens}
                for v in report.verifier_results
            ],
            "tokens": sum(r.tokens for r in report.agent_results) + sum(v.tokens for v in report.verifier_results),
            "symbols_resolved": len(report.context.symbols),
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    typer.echo(f"hunks: {len(report.context.hunks)}   symbols resolved: {len(report.context.symbols)}")
    def _cost(elapsed_ms: int, tokens: int) -> str:
        return f"{elapsed_ms} ms" + (f", {tokens} tokens" if tokens else "")

    for r in report.agent_results:
        status = f"ERROR: {r.error}" if r.error else f"{len(r.findings)} findings"
        typer.echo(f"  [{r.agent_id}] {status} ({_cost(r.elapsed_ms, r.tokens)})")
    for v in report.verifier_results:
        status = f"ERROR (fail-open, nothing dropped): {v.error}" if v.error else f"{len(report.dropped)} dropped"
        typer.echo(f"  [verifier {v.verifier_id}] {status} ({_cost(v.elapsed_ms, v.tokens)})")
    total_tokens = sum(r.tokens for r in report.agent_results) + sum(v.tokens for v in report.verifier_results)
    if total_tokens:
        typer.echo(f"  total: {total_tokens} tokens")
    typer.echo("")
    placed = [f for f in report.findings if not f.unstructured]
    unstructured = [f for f in report.findings if f.unstructured]
    if not placed:
        typer.echo("No findings.")
    for f in placed:
        merged = f" (+{len(f.merged_from)} agents agree)" if f.merged_from else ""
        flag = " ⚑" if f.flagged else ""
        typer.echo(f"{f.severity.value.upper():8} {f.file}:{f.line}  [{f.category.value}] {f.message}{merged}{flag}")
        if f.reasoning:
            typer.echo(f"         {f.reasoning}")
        if f.flagged:
            typer.echo(f"         ⚑ verifier: {f.dropped_reason[:160]}")
    if report.notes:
        typer.echo("\nnot reviewed:")
        for n in report.notes:
            typer.echo(f"  {n}")
    if unstructured:
        typer.echo(f"\nunstructured output ({len(unstructured)}, not scored, not posted inline):")
        for f in unstructured:
            typer.echo(f"  [{f.source_agent}] {f.message[:200]}")
    if report.dropped:
        typer.echo(f"\ndropped by verifier ({len(report.dropped)}):")
        for f in report.dropped:
            typer.echo(f"  {f.file}:{f.line} [{f.category.value}] {f.message[:100]}")
            typer.echo(f"      -> {f.dropped_reason[:160]}")


@app.command()
def review(
    repo: Path = typer.Option(Path("."), help="Path to the repo checkout."),
    diff_file: Path | None = typer.Option(None, help="Unified diff to review. Defaults to `git diff <base>`."),
    base: str | None = typer.Option(None, help="Git ref to diff against when --diff-file is not given."),
    config: Path | None = typer.Option(None, help="Path to .review-agent.yaml (defaults to <repo>/.review-agent.yaml)."),
    no_context: bool = typer.Option(False, help="Ablation: skip the context engine, feed agents diff only."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    fail_on_critical: bool = typer.Option(False, help="Exit 1 if any critical finding (for CI gating)."),
    post: str = typer.Option("none", help="Post findings to the MR/PR: none|auto|gitlab|github."),
    audit_dir: Path | None = typer.Option(None, "--audit-dir", help="Write a per-run JSON record (models, tokens, verdicts, findings) here."),
) -> None:
    repo = repo.resolve()
    cfg = load_config(config or (repo / ".review-agent.yaml"))
    agents = build_agents(cfg, repo_root=repo)
    verifiers = build_verifiers(cfg)
    provider = _make_provider(post)  # fail fast on missing env before spending LLM tokens
    diff_text = _read_diff(diff_file, repo, base)

    report = asyncio.run(
        run_review(
            diff_text,
            repo,
            agents,
            repo_rules=cfg.rules,
            use_context_engine=not no_context,
            per_agent_timeout_s=cfg.per_agent_timeout_s,
            line_window=cfg.aggregation.line_window,
            max_per_category=cfg.aggregation.max_per_category,
            max_total=cfg.aggregation.max_total,
            verifiers=verifiers,
            verifier_mode=cfg.verifier_mode,
            exclude=cfg.exclude,
            limits=cfg.limits,
        )
    )
    _emit(report, as_json)

    if audit_dir is not None:
        source = str(diff_file) if diff_file else f"git diff {base or 'HEAD'} in {repo}"
        written = write_audit(report, audit_dir, agents, verifiers, cfg, source)
        typer.echo(f"audit: {written}", err=True)

    if provider is not None:
        posted = provider.post_findings(report.findings, report.context.hunks, notes=report.notes)
        typer.echo(
            f"posted: {posted.inline_posted} inline, {posted.summary_posted} summary note"
            + (f", {len(posted.errors)} error(s)" if posted.errors else ""),
            err=True,
        )
        for e in posted.errors:
            typer.echo(f"  {e}", err=True)

    if fail_on_critical and has_blocking_findings(report.findings):
        sys.exit(1)


@app.command()
def init(
    path: Path = typer.Option(Path(".review-agent.yaml"), help="Where to write the config."),
    force: bool = typer.Option(False, help="Overwrite an existing file."),
) -> None:
    """Write a starter .review-agent.yaml (generalist + verifier, specialists opt-in)."""
    from importlib.resources import files

    if path.exists() and not force:
        raise typer.BadParameter(f"{path} exists; pass --force to overwrite")
    template = files("review_agent.templates").joinpath("default.yaml").read_text(encoding="utf-8")
    path.write_text(template, encoding="utf-8")
    typer.echo(f"wrote {path}  (edit model/api_base/api_key_env, then: review-agent review --base main)")


@app.command()
def demo(
    repo: Path = typer.Option(Path("."), help="Repo checkout to run the zero-setup demo against."),
    diff_file: Path | None = typer.Option(None, help="Diff to use; defaults to `git diff HEAD`."),
) -> None:
    """Run the full pipeline with deterministic FakeAgents. No API key needed."""
    repo = repo.resolve()
    diff_text = _read_diff(diff_file, repo, None)
    agents = [FakeAgent("fake-logic", Category.LOGIC), FakeAgent("fake-security", Category.SECURITY)]
    report = asyncio.run(run_review(diff_text, repo, agents))
    _emit(report, as_json=False)


def _load_suite(suite: str, cases_file: Path | None):
    from review_agent.bench.cases import load_cases, read_cases

    if cases_file is not None:
        return read_cases(cases_file)
    try:
        return load_cases(suite)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _roster(config: Path | None):
    """(agents, verifiers, roster label, default arms) for a config path, or the zero-cost default."""
    from review_agent.agents.heuristic_agent import HeuristicAgent

    if config is not None:
        cfg = load_config(config)
        agents = build_agents(cfg)
        verifiers = build_verifiers(cfg)
        label = ", ".join(f"{a.agent_id}({a.role})" for a in agents) + "".join(f", {v.verifier_id}(verifier)" for v in verifiers)
        return agents, verifiers, label, ("A,B,C,D,E,F" if verifiers else "A,B,C,D")
    return [HeuristicAgent(role="baseline")], [], "heuristic(baseline) (default; pass --config for a real roster)", "A,B"


def _arm_keys(arms: str):
    from review_agent.bench.runner import ARMS

    keys = [a.strip().upper() for a in arms.split(",") if a.strip()]
    bad = [a for a in keys if a not in ARMS]
    if bad:
        raise typer.BadParameter(f"unknown arm(s) {bad}; choose from {list(ARMS)}")
    return keys


@app.command()
def bench(
    config: Path | None = typer.Option(None, help="Agent roster yaml. Default: the zero-cost heuristic agent as baseline."),
    arms: str | None = typer.Option(None, help="Comma-separated subset of A-F. Default: A-D with --config (+E,F if it declares a verifier), A,B without."),
    suite: str = typer.Option("builtin", help="Built-in case suite: builtin | crossfile | all."),
    cases_file: Path | None = typer.Option(None, "--cases", help="JSON from `review-agent mine`; overrides --suite."),
    repeats: int = typer.Option(1, help="Run the whole benchmark N times and report mean [min,max]. Models are not deterministic."),
    concurrency: int = typer.Option(4, help="Cases reviewed in parallel per arm."),
    line_tol: int = typer.Option(3, help="Line tolerance when matching findings to ground truth."),
    seed: int = typer.Option(0, help="Bootstrap / shuffle seed."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    export_labels_to: Path | None = typer.Option(None, "--export-labels", help="Write a shuffled, provenance-stripped JSONL (+ .key.json) for blind labeling (last repeat)."),
) -> None:
    """Run the ablation study on a ground-truth benchmark suite."""
    from review_agent.bench.runner import (
        export_labels, format_repeats_table, format_table, repeats_json, results_json, run_bench_repeated,
    )

    agents, verifiers, roster, default_arms = _roster(config)
    arm_keys = _arm_keys(arms or default_arms)
    cases = _load_suite(suite, cases_file)
    try:
        runs = run_bench_repeated(cases, agents, arm_keys, repeats=repeats, line_tol=line_tol,
                                  verifiers=verifiers, concurrency=concurrency)
    except ValueError as exc:  # an arm with no agents of the required role
        raise typer.BadParameter(str(exc)) from exc
    results = runs[-1]

    source = str(cases_file) if cases_file else f"suite {suite}"
    meta = {"roster": roster, "source": source, "cases": len(cases), "repeats": repeats}
    if as_json:
        payload = {**meta, **results_json(results)} if repeats == 1 else {"meta": meta, **repeats_json(runs)}
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"roster: {roster}")
        typer.echo(f"{source}: {len(cases)} cases ({sum(1 for c in cases if c.clean)} clean), truths: {sum(len(c.truths) for c in cases)}, line_tol: {line_tol}\n")
        typer.echo(format_table(results) if repeats == 1 else format_repeats_table(runs))

    if export_labels_to is not None:
        out, key = export_labels(results, export_labels_to, seed=seed)
        typer.echo(f"\nlabels: {out}\nkey:    {key}", err=True)


@app.command()
def report(
    archives: list[Path] = typer.Argument(..., help="One or more `bench --json` archives (single runs or --repeats)."),
) -> None:
    """Render archived benchmark results side by side, by arm."""
    from review_agent.bench.runner import format_report, load_results

    loaded = [load_results(p) for p in archives]
    for item in loaded:
        meta = item.get("meta") or {}
        if meta:
            typer.echo(f"{item['label']}: " + ", ".join(f"{k}={v}" for k, v in meta.items() if k in ("roster", "source", "cases", "repeats")))
    typer.echo("")
    typer.echo(format_report(loaded))


@app.command()
def compare(
    configs: list[Path] = typer.Option(..., "--config", help="Candidate roster yaml (repeat the flag)."),
    arms: str = typer.Option("B", help="Arms to run for every config, e.g. B or B,F."),
    suite: str = typer.Option("builtin", help="Built-in case suite: builtin | crossfile | all."),
    cases_file: Path | None = typer.Option(None, "--cases", help="JSON from `review-agent mine`; overrides --suite."),
    repeats: int = typer.Option(2, help="Runs per config; the spread is the point."),
    concurrency: int = typer.Option(4),
    line_tol: int = typer.Option(3),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run several candidate configs on one case set and print one table: which model/roster, on YOUR code."""
    from review_agent.bench.runner import repeats_json, run_bench_repeated, summarize_repeats

    cases = _load_suite(suite, cases_file)
    arm_keys = _arm_keys(arms)
    rows: list[tuple[str, str, dict]] = []
    payload: dict = {}
    for cfg_path in configs:
        agents, verifiers, roster, _ = _roster(cfg_path)
        try:
            runs = run_bench_repeated(cases, agents, arm_keys, repeats=repeats, line_tol=line_tol,
                                      verifiers=verifiers, concurrency=concurrency)
        except ValueError as exc:
            raise typer.BadParameter(f"{cfg_path}: {exc}") from exc
        summary = summarize_repeats(runs)
        payload[str(cfg_path)] = {"roster": roster, **repeats_json(runs)}
        for arm in arm_keys:
            rows.append((cfg_path.name, arm, summary[arm]))

    if as_json:
        typer.echo(json.dumps({"source": str(cases_file) if cases_file else f"suite {suite}", "cases": len(cases),
                               "repeats": repeats, "configs": payload}, indent=2))
        return

    def cell(s, m, fmt):
        mean, lo, hi = s[m]
        return (fmt % mean) if repeats == 1 else f"{fmt % mean} [{fmt % lo},{fmt % hi}]"

    head = f"{'config':<30}{'arm':<4}{'recall(strict)':<20}{'precision':<20}{'FP/clean':<14}{'find':<14}{'dup':<10}{'tokens':<22}{'err'}"
    typer.echo(f"cases: {len(cases)} ({sum(1 for c in cases if c.clean)} clean), truths: {sum(len(c.truths) for c in cases)}, repeats: {repeats}\n")
    typer.echo(head)
    typer.echo("-" * len(head))
    for name, arm, s in rows:
        typer.echo(f"{name[:29]:<30}{arm:<4}{cell(s, 'recall_strict', '%.2f'):<20}{cell(s, 'precision', '%.2f'):<20}"
                   f"{cell(s, 'fp_on_clean', '%.0f'):<14}{cell(s, 'findings', '%.0f'):<14}{cell(s, 'duplicates', '%.0f'):<10}"
                   f"{cell(s, 'tokens', '%.0f'):<22}{cell(s, 'agent_errors', '%.0f')}")


@app.command("label-import")
def label_import(
    pool: Path = typer.Option(..., "--pool", help="Labels JSONL from `bench --export-labels`; .key.json beside it."),
    answers: Path = typer.Option(..., "--answers", help="CSV `label_id,answer` with answer yes | no | dup."),
    cases_file: Path | None = typer.Option(None, "--cases", help="Cases JSON the pool came from; `yes` answers become new truths."),
    out: Path | None = typer.Option(None, help="Where to write the cases JSON with added truths (requires --cases)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Fold human answers into the benchmark: human precision per arm, machine/human disagreements, new truths."""
    from review_agent.bench.cases import read_cases, write_cases
    from review_agent.bench.runner import format_label_report, import_labels, load_pool, read_answers

    rows = load_pool(pool)
    try:
        ans = read_answers(answers)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    cases = read_cases(cases_file) if cases_file else None
    rep, updated = import_labels(rows, ans, cases)
    if out is not None:
        if cases is None:
            raise typer.BadParameter("--out needs --cases")
        write_cases(updated, out)
    if as_json:
        typer.echo(json.dumps({"by_arm": rep.by_arm, "human_precision": {a: rep.human_precision(a) for a in rep.by_arm},
                               "disagreements": rep.disagreements, "new_truths": rep.new_truths}, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"answers: {len(ans)} of {len(rows)} pool rows labeled\n")
        typer.echo(format_label_report(rep))
        if out is not None:
            typer.echo(f"\nwrote {out} ({sum(len(c.truths) for c in updated)} truths)")


@app.command("bench-verifier")
def bench_verifier(
    pool: list[Path] = typer.Option(..., "--pool", help="Labels JSONL from `bench --export-labels` (repeatable); its .key.json must sit beside it."),
    config: Path = typer.Option(..., help="Roster yaml declaring an agent with role: verifier."),
    suite: str = typer.Option("builtin", help="Case suite the pool was produced from: builtin | crossfile | all."),
    cases_file: Path | None = typer.Option(None, "--cases", help="JSON from `review-agent mine`; overrides --suite."),
    no_context: bool = typer.Option(False, help="Hand the verifier the diff only."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Replay labeled first-stage findings through the verifier and score what it drops."""
    from review_agent.bench.runner import format_verifier_eval, load_pool, run_verifier_eval_sync

    verifiers = build_verifiers(load_config(config))
    if not verifiers:
        raise typer.BadParameter("config declares no agent with role: verifier")
    rows = [r for p in pool for r in load_pool(p)]
    ev = run_verifier_eval_sync(rows, _load_suite(suite, cases_file), verifiers[0], use_context=not no_context)
    if as_json:
        typer.echo(json.dumps({k: (v if not isinstance(v, float) else round(v, 4)) for k, v in {
            **ev.__dict__, "precision_before": ev.precision_before, "precision_after": ev.precision_after,
            "fp_drop_rate": ev.fp_drop_rate, "truth_loss_rate": ev.truth_loss_rate}.items()}, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"verifier: {verifiers[0].verifier_id}   pools: {', '.join(str(p) for p in pool)}")
        typer.echo(format_verifier_eval(ev))


@app.command()
def mine(
    repo: Path = typer.Option(..., help="Path to a local git clone."),
    mode: str = typer.Option("recent", help="recent = latest commits as clean cases; fixes = SZZ-lite bug-introducing commits with blamed truths."),
    n: int = typer.Option(15, help="How many cases to produce."),
    max_lines: int = typer.Option(400, help="Skip commits whose Python diff exceeds this many lines."),
    out: Path = typer.Option(..., help="Where to write the cases JSON (use with `bench --cases`)."),
) -> None:
    """Mine benchmark cases from a real repository's history."""
    from review_agent.bench.cases import write_cases
    from review_agent.bench.mine import fix_pair_cases, recent_cases

    repo = repo.resolve()
    if mode == "recent":
        cases = recent_cases(str(repo), n=n, max_lines=max_lines)
    elif mode == "fixes":
        cases = fix_pair_cases(str(repo), n=n, max_lines=max_lines)
    else:
        raise typer.BadParameter("mode must be 'recent' or 'fixes'")
    write_cases(cases, out)
    truths = sum(len(c.truths) for c in cases)
    typer.echo(f"wrote {len(cases)} cases ({truths} truths) to {out}")
    for c in cases:
        typer.echo(f"  {c.id}  files={len(c.after)}  truths={len(c.truths)}  {c.note[:90]}")


if __name__ == "__main__":
    app()
