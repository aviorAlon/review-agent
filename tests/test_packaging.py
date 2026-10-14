import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from review_agent.cli import app

ROOT = Path(__file__).parent.parent


def test_init_writes_the_packaged_template_which_matches_the_repo_example(tmp_path):
    runner = CliRunner()
    target = tmp_path / ".review-agent.yaml"
    result = runner.invoke(app, ["init", "--path", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == (ROOT / ".review-agent.example.yaml").read_text(encoding="utf-8")

    again = runner.invoke(app, ["init", "--path", str(target)])
    assert again.exit_code != 0 and "exists" in again.output
    forced = runner.invoke(app, ["init", "--path", str(target), "--force"])
    assert forced.exit_code == 0


def test_init_template_loads_as_a_valid_config(tmp_path):
    from review_agent.config import load_config

    CliRunner().invoke(app, ["init", "--path", str(tmp_path / "c.yaml")])
    cfg = load_config(tmp_path / "c.yaml")
    enabled = {a.id: a.role for a in cfg.agents if a.enabled}
    assert enabled == {"generalist": "baseline", "verifier": "verifier"}
    assert cfg.verifier_mode == "flag" and cfg.limits.max_files == 40 and "*.lock" in cfg.exclude


def test_help_renders_when_stdout_is_piped():
    # Regression for rich's legacy-Windows renderer crashing on a closed pipe.
    proc = subprocess.run([sys.executable, "-m", "review_agent.cli", "review", "--help"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "--fail-on-critical" in proc.stdout
