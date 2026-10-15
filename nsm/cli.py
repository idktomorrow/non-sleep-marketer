"""명령줄 진입점: `uv run nsm --help`."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="논슬립 마케터: 로컬 AI 가상 인플루언서 마케터", no_args_is_help=True)


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="실제 API를 한 번씩 호출해 키를 확인(비용 아주 조금)"
    ),
    with_image: bool = typer.Option(
        False, "--with-image", help="실제 API 확인 + 이미지 1장 생성(약 $0.05). --live를 포함"
    ),
    live_only: bool = typer.Option(
        False, "--live-only", help="오프라인 점검은 건너뛰고 실제 API 확인만"
    ),
) -> None:
    """환경을 진단합니다 (Python, DB, 설정, API 키, 브라우저, 폰트)."""
    from nsm.doctor import run_checks

    live = live or with_image or live_only
    checks = [] if live_only else run_checks()
    if live:
        from nsm.doctor_live import cost_sink, run_live_checks

        # 실제 호출 비용도 오늘 비용에 들어가게 DB에 기록한다(배치 Q1). DB를 못 열면 기록 없이 점검만.
        with cost_sink() as sink:
            checks += run_live_checks(with_image=with_image, sink=sink)
    for c in checks:
        typer.echo(f"[{c.status:4}] {c.name}: {c.detail}")
    if any(c.status == "FAIL" for c in checks):
        raise typer.Exit(1)


@app.command()
def init() -> None:
    """설정 마법사: 위험 고지 → 브랜드 → 페르소나 → API 키."""
    from nsm.config.wizard import run_wizard

    run_wizard()


@app.command()
def run(
    once: bool = typer.Option(False, "--once", help="잡 하나만 1회 실행하고 종료"),
    job: str = typer.Option(
        "",
        "--job",
        help="--once와 함께: collect|generate|publish|reply|metrics|health|weekly_report",
    ),
) -> None:
    """스케줄러와 대시보드를 한 프로세스로 실행합니다."""
    from nsm.runtime import run_forever, run_job_once

    if once:
        if not job:
            raise typer.BadParameter("--once에는 --job이 필요합니다")
        run_job_once(job)
    else:
        run_forever()


@app.command("render-preview")
def render_preview(
    deck: Path = typer.Argument(..., exists=True, help="카드 덱 JSON"),
    out: Path | None = typer.Option(None, "--out", help="기본: <NSM_HOME>/preview/render"),
    style: str | None = typer.Option(
        None,
        "--style",
        help="카드 스타일 clean|photo|illust. 기본: 설정 파일의 brand.card_style(없으면 clean)",
    ),
) -> None:
    """카드 덱 JSON을 PNG로 렌더합니다 (렌더러는 배치 S-α, 스타일은 S8)."""
    from nsm.config.paths import preview_dir
    from nsm.config.schema import CARD_STYLES
    from nsm.domain.models import CardDeck
    from nsm.render import get_renderer

    if style is None:
        style = _configured_card_style()
    if style not in CARD_STYLES:
        raise typer.BadParameter(f"{', '.join(CARD_STYLES)} 중 하나", param_hint="--style")
    d = CardDeck.model_validate_json(deck.read_text(encoding="utf-8"))
    paths = get_renderer(style=style).render(d, out or preview_dir() / "render")
    for p in paths:
        typer.echo(str(p))


def _configured_card_style() -> str:
    from nsm.config.store import load_config

    try:
        return load_config().brand.card_style
    except Exception:  # noqa: BLE001  설정 전(nsm init 전)에도 미리보기는 된다
        return "clean"


@app.command("install-autostart")
def install_autostart() -> None:
    """Windows 로그온 시 `nsm run`이 자동 실행되도록 작업 스케줄러에 등록합니다."""
    from nsm.scheduler.autostart import install

    typer.echo(install())


@app.command("uninstall-autostart")
def uninstall_autostart() -> None:
    """자동 실행 등록을 해제합니다."""
    from nsm.scheduler.autostart import uninstall

    typer.echo(uninstall())


if __name__ == "__main__":
    app()
