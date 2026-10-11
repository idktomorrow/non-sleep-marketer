from __future__ import annotations

from typer.testing import CliRunner

from nsm.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["doctor", "init", "run", "render-preview", "install-autostart"]:
        assert cmd in result.output


def test_doctor_runs_without_config(nsm_home) -> None:
    result = runner.invoke(app, ["doctor"])
    assert "Python 3.12" in result.output
    assert "DuckDB" in result.output
    assert "[FAIL] DuckDB" not in result.output
