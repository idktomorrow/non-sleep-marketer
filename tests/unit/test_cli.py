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


def test_render_preview_style_option(nsm_home, tmp_path, monkeypatch) -> None:
    """S8: --style로 스타일을 고르고, 없으면 설정의 brand.card_style(설정 전이면 clean)."""
    from pathlib import Path

    import nsm.render as render

    used: list[str] = []

    class Fake:
        def render(self, deck, out_dir):
            return [out_dir / "slide-01.png"]

    monkeypatch.setattr(render, "get_renderer", lambda style="clean": used.append(style) or Fake())
    deck = Path(__file__).resolve().parents[1] / "fixtures" / "decks" / "info.json"
    out = str(tmp_path)
    assert (
        runner.invoke(
            app, ["render-preview", str(deck), "--out", out, "--style", "photo"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["render-preview", str(deck), "--out", out]).exit_code == 0
    bad = runner.invoke(app, ["render-preview", str(deck), "--out", out, "--style", "neon"])
    assert bad.exit_code != 0
    assert used == ["photo", "clean"]


def test_keyword_option_only_with_keyword_job(nsm_home, monkeypatch) -> None:
    """C15: --job keyword에는 --keyword가 필요하고, 다른 잡에는 붙일 수 없다(조용히 무시하지 않음)."""
    import nsm.runtime as runtime

    called: list = []
    monkeypatch.setattr(runtime, "run_job_once", lambda job, arg=None: called.append((job, arg)))
    assert runner.invoke(app, ["run", "--once", "--job", "keyword"]).exit_code != 0
    assert (
        runner.invoke(app, ["run", "--once", "--job", "collect", "--keyword", "굿즈"]).exit_code
        != 0
    )
    assert called == []
    ok = runner.invoke(app, ["run", "--once", "--job", "keyword", "--keyword", "치이카와 굿즈"])
    assert ok.exit_code == 0 and called == [("keyword", "치이카와 굿즈")]
