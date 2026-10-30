"""R2: 문서 색인 잡(하루 상한·키 없음·한도·실행 기록·비용 상한)과 설정 화면 한 줄, nsm ask 출력. 네트워크 없음."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.engine.context import JobContext
from nsm.engine.runner import JOBS, run_job
from nsm.scheduler.app import job_triggers
from nsm.store.repo import Repository
from tests.fakes import FakeEmbedder, FakeNotifier


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    for i in range(3):
        (root / "adr" / f"000{i + 1}-x.md").write_text(
            f"# {i}번 결정\n\n## 결정\n\n본문 {i}입니다.\n", "utf-8"
        )
    return root


@pytest.fixture
def ctx(db, clock, app_config, docs: Path) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={},
        embedder=FakeEmbedder(),
        docs_root=docs,
    )


def _last_run(ctx: JobContext) -> dict:
    return next(r for r in ctx.repo.recent_runs(10) if r["job"] == "index")


def test_index_job_is_scheduled_daily_at_setting_time(app_config) -> None:
    assert "index" in JOBS
    t = str(job_triggers(app_config)["index"])
    assert "hour='20'" in t and "minute='0'" in t
    app_config.settings.rag.index_time = "21:30"
    t = str(job_triggers(app_config)["index"])
    assert "hour='21'" in t and "minute='30'" in t


def test_job_indexes_and_records_run_log(ctx: JobContext) -> None:
    res = run_job("index", ctx)
    assert res.ok and res.counts["chunks"] == 3 and res.counts["sent"] == 3
    assert res.counts["without_vector"] == 0 and "seconds" in res.counts
    msg = _last_run(ctx)["message"]
    assert "조각 3개, 벡터 없음 0개, 이번에 보냄 3개" in msg
    again = run_job("index", ctx)
    assert again.counts["sent"] == 0 and ctx.notifier.calls == []  # 증분, 알림 없음


def test_daily_cap_is_respected(ctx: JobContext, app_config) -> None:
    app_config.settings.rag.embed_max_per_run = 2
    res = run_job("index", ctx)
    assert res.ok and res.counts["sent"] == 2 and res.counts["without_vector"] == 1
    assert "내일" in res.counts["note"]
    nxt = run_job("index", ctx)
    assert nxt.counts["sent"] == 1 and nxt.counts["without_vector"] == 0


def test_no_key_ends_quietly_with_chunks_saved(ctx: JobContext) -> None:
    ctx.embedder = None
    res = run_job("index", ctx)
    assert res.ok and res.counts["chunks"] == 3 and res.counts["without_vector"] == 3
    assert "키 없음" in res.counts["note"] and ctx.notifier.calls == []
    ctx.embedder = FakeEmbedder()  # 키가 생기면 다음 날 벡터 없는 조각만
    assert run_job("index", ctx).counts["sent"] == 3


def test_rate_limit_stops_quietly_and_resumes(ctx: JobContext) -> None:
    ctx.embedder = FakeEmbedder(fail_after=0)
    res = run_job("index", ctx)
    assert res.ok and res.counts["sent"] == 0 and res.counts["without_vector"] == 3
    assert "429" in res.counts["note"] and ctx.notifier.calls == []
    ctx.embedder = FakeEmbedder()
    assert run_job("index", ctx).counts["without_vector"] == 0


def test_daily_cost_cap_skips_embedding(ctx: JobContext) -> None:
    ctx.repo.record_cost("llm", "anthropic", 999.0, "x")
    emb = ctx.embedder
    res = run_job("index", ctx)
    assert res.ok and emb.calls == [] and "비용 상한" in res.counts["note"]


def test_settings_page_shows_doc_index_line(
    db, clock, app_config, ctx: JobContext, tmp_path
) -> None:
    from nsm.app.server import AppState, create_app
    from tests.app.test_dashboard import _run_now

    run_job("index", ctx)
    state = AppState(
        repo=ctx.repo,
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
    )
    html = TestClient(create_app(state)).get("/settings").text
    assert "문서 색인: 조각 3개, 벡터 없음 0개, 마지막 실행 10-11" in html


def test_ask_prints_hits_with_scores_and_paths(app_config, ctx: JobContext, tmp_path: Path) -> None:
    from nsm.rag.command import run_ask
    from nsm.store.db import Database

    path = tmp_path / "ask.duckdb"
    db = Database(path)
    c2 = JobContext(
        config=app_config,
        repo=Repository(db, ctx.clock),
        clock=ctx.clock,
        notifier=FakeNotifier(),
        adapters={},
        embedder=FakeEmbedder(),
        docs_root=ctx.docs_root,
    )
    run_job("index", c2)
    db.close()
    out: list[str] = []
    app_config.settings.rag.min_score = 0.0
    code = run_ask(
        app_config,
        path,
        "1번 결정 본문",
        k=2,
        secrets=object(),
        embedder_factory=lambda *a: FakeEmbedder(),
        echo=out.append,
    )
    assert code == 0 and out[0] == "질문: 1번 결정 본문"
    assert out[2].startswith("1. [") and "adr/0002-x.md > 1번 결정 > 결정" in out[2]
    assert len([line for line in out if line[:2] in ("1.", "2.")]) == 2
    out.clear()
    app_config.settings.rag.min_score = 0.999
    run_ask(
        app_config,
        path,
        "점심 메뉴",
        secrets=object(),
        embedder_factory=lambda *a: FakeEmbedder(),
        echo=out.append,
    )
    assert any("근거 없음" in line for line in out)
    out.clear()
    assert (
        run_ask(
            app_config,
            path,
            "x",
            secrets=object(),
            embedder_factory=lambda *a: None,
            echo=out.append,
        )
        == 1
    )
    assert "Gemini 키가 없어" in out[0]
