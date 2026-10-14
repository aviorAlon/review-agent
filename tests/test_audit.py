import json

from typer.testing import CliRunner

from review_agent.cli import app


def test_review_writes_an_audit_record_with_run_id(tmp_path, fake_repo, sample_diff):
    (fake_repo / ".review-agent.yaml").write_text(
        'version: "1"\nagents:\n  - id: h\n    type: heuristic\n    role: baseline\n', encoding="utf-8")
    diff = tmp_path / "change.diff"
    diff.write_text(sample_diff, encoding="utf-8")
    audit_dir = tmp_path / "audit"

    result = CliRunner().invoke(app, ["review", "--repo", str(fake_repo), "--diff-file", str(diff),
                                      "--json", "--audit-dir", str(audit_dir)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"]

    (record_path,) = audit_dir.glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record_path.stem == record["run_id"] == payload["run_id"]
    assert record["config"]["verifier_mode"] == "flag"
    assert record["config"]["agents"] == [{"id": "h", "role": "baseline", "backend": "HeuristicAgent", "model": ""}]
    assert record["input"]["files"] == ["app/auth.py"] and record["input"]["symbols_resolved"] == 1
    assert record["agents"][0]["agent_id"] == "h" and record["agents"][0]["error"] is None
    assert "timestamp" in record and record["tokens"] == 0
    assert set(record) >= {"findings", "dropped", "verifiers", "notes"} - {"notes"}


def test_unsupported_language_is_reported_in_notes(tmp_path):
    (tmp_path / "x.rb").write_text("def f\n  1\nend\n", encoding="utf-8")
    (tmp_path / ".review-agent.yaml").write_text(
        'version: "1"\nagents:\n  - id: h\n    type: heuristic\n    role: baseline\n', encoding="utf-8")
    diff = tmp_path / "c.diff"
    diff.write_text("--- a/x.rb\n+++ b/x.rb\n@@ -1,3 +1,3 @@\n def f\n-  1\n+  2\n end\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["review", "--repo", str(tmp_path), "--diff-file", str(diff), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any("diff-only context" in n and "x.rb" in n for n in payload["notes"])
