"""R4: 색인 잡이 출처 셋(문서 → 수집 자료 → 우리 글)을 차례로 돌고 하루 상한을 나눠 쓰며, 한 출처가 실패해도 계속한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.engine.context import JobContext
from nsm.engine.runner import run_job
from nsm.rag.command import IndexInputError, run_index
from nsm.store.repo import Repository
from tests.fakes import FakeEmbedder, FakeNotifier
from tests.unit.test_rag_sources_db import add_item, add_post


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
    # 수집 자료 2건(각 1조각), 우리 글 1건(본문 절 + 카드 문구 절 = 2조각)
    add_item(db, "A", now=clock.now())
    add_item(db, "B", now=clock.now())
    add_post(db, "C1", "threads", now=clock.now())
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={},
        embedder=FakeEmbedder(),
        docs_root=docs,
    )


def _kinds_in_send_order(emb: FakeEmbedder) -> list[str]:
    out = []
    for batch in emb.calls:
        for t in batch:
            k = (
                "collected"
                if t.startswith("수집 자료")
                else "posts"
                if t.startswith("우리 글")
                else "docs"
            )
            if not out or out[-1] != k:
                out.append(k)
    return out


def _by_kind(ctx: JobContext) -> dict[str, tuple[int, int]]:
    rows = ctx.repo.db.execute(
        "SELECT source_kind, count(*), count(*) FILTER (WHERE embedding IS NULL) FROM doc_chunks GROUP BY 1"
    )
    return {k: (int(n), int(z)) for k, n, z in rows}


def test_job_runs_three_sources_in_order(ctx: JobContext) -> None:
    res = run_job("index", ctx)
    assert res.ok, res.errors
    assert _by_kind(ctx) == {"docs": (3, 0), "collected": (2, 0), "posts": (2, 0)}
    assert res.counts["chunks"] == 7 and res.counts["sent"] == 7
    assert res.counts["by_source"] == "문서 3 · 수집 2 · 우리 글 2"
    assert _kinds_in_send_order(ctx.embedder) == ["docs", "collected", "posts"]
    msg = next(r for r in ctx.repo.recent_runs(5) if r["job"] == "index")["message"]
    assert "출처별 문서 3 · 수집 2 · 우리 글 2" in msg


def test_daily_cap_is_shared_docs_first(ctx: JobContext, app_config) -> None:
    app_config.settings.rag.embed_max_per_run = 4  # 문서 3 + 수집 1까지, 나머지는 벡터 없이
    res = run_job("index", ctx)
    assert res.ok and res.counts["sent"] == 4
    assert _by_kind(ctx) == {"docs": (3, 0), "collected": (2, 1), "posts": (2, 2)}
    assert "내일" in res.counts["note"] and "3조각" in res.counts["note"]
    nxt = run_job("index", ctx)  # 다음 날 같은 상한으로 이어서
    assert nxt.counts["sent"] == 3 and _by_kind(ctx)["posts"] == (2, 0)


def test_today_ledger_is_shared_across_sources(ctx: JobContext, app_config) -> None:
    from nsm.rag.budget import add_used, used_today

    app_config.settings.rag.embed_daily_limit = 10
    add_used(ctx.repo, ctx.clock.now(), 5)  # 질문 등으로 오늘 이미 5건
    res = run_job("index", ctx)
    assert res.counts["sent"] == 5 and used_today(ctx.repo, ctx.clock.now()) == 10


def test_empty_docs_folder_does_not_stop_other_sources(ctx: JobContext, tmp_path: Path) -> None:
    run_job("index", ctx)
    ctx.docs_root = tmp_path / "없는-폴더"  # 저장된 docs 조각이 있는데 문서 0개: EmptySourceError
    res = run_job("index", ctx)
    assert not res.ok and any(e.startswith("문서: ") for e in res.errors)
    assert res.counts["by_source"] == "문서 실패 · 수집 2 · 우리 글 2"
    assert _by_kind(ctx)["docs"] == (3, 0)  # 지우지 않았다


def test_fresh_install_without_collected_or_posts_indexes_docs(db, clock, app_config, docs) -> None:
    ctx = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={},
        embedder=FakeEmbedder(),
        docs_root=docs,
    )
    res = run_job("index", ctx)
    assert res.ok and res.counts["chunks"] == 3 and res.counts["sent"] == 3
    assert res.counts["by_source"] == "문서 3 · 수집 0 · 우리 글 0"


def test_a_broken_source_is_skipped(ctx: JobContext, monkeypatch) -> None:
    from nsm.rag import sources_db

    def boom(self):
        raise RuntimeError("DB 읽기 실패")

    monkeypatch.setattr(sources_db.CollectedSource, "documents", boom)
    res = run_job("index", ctx)
    assert not res.ok and res.errors == ["수집: DB 읽기 실패"]
    assert set(_by_kind(ctx)) == {"docs", "posts"}


def test_rate_limit_stops_sending_for_later_sources(ctx: JobContext) -> None:
    emb = FakeEmbedder(fail_after=0)
    tries: list[int] = []
    real = emb.embed
    emb.embed = lambda texts: (tries.append(len(texts)), real(texts))[1]
    ctx.embedder = emb
    res = run_job("index", ctx)
    assert res.ok and res.counts["sent"] == 0 and "429" in res.counts["note"]
    assert tries == [3]  # 문서에서 한 번 막히면 뒤 출처는 보내 보지도 않는다
    assert _by_kind(ctx) == {"docs": (3, 3), "collected": (2, 2), "posts": (2, 2)}


def test_sources_setting_controls_order_and_choice(ctx: JobContext, app_config) -> None:
    app_config.settings.rag.sources = ["posts", "docs"]
    res = run_job("index", ctx)
    assert res.counts["by_source"] == "우리 글 2 · 문서 3"
    assert "collected" not in _by_kind(ctx)
    assert _kinds_in_send_order(ctx.embedder) == ["posts", "docs"]


# ------------------------------------------------------------------ nsm index --source


def _cli(app_config, db_path: Path, docs: Path, **kw) -> tuple[int, list[str]]:
    out: list[str] = []
    code = run_index(
        app_config,
        db_path,
        docs=docs,
        secrets=None,
        embedder_factory=lambda cfg, secrets, repo: FakeEmbedder(),
        echo=out.append,
        **kw,
    )
    return code, out


def _seed_file_db(path: Path, clock) -> None:
    from nsm.store.db import Database

    d = Database(path)
    add_item(d, "A", now=clock.now())
    add_post(d, "C1", "threads", now=clock.now())
    d.close()


def test_cli_source_option(app_config, docs: Path, tmp_path: Path, clock) -> None:
    db_path = tmp_path / "x.duckdb"
    _seed_file_db(db_path, clock)
    code, out = _cli(app_config, db_path, docs, source="collected")
    assert code == 0 and any(line.startswith("[수집] 파일 1개 · 조각 1개") for line in out)
    assert not any(line.startswith("[문서]") for line in out)
    code, out = _cli(app_config, db_path, docs)  # 기본 all
    assert code == 0 and [line[:4] for line in out if line.startswith("[")] == [
        "[문서]",
        "[수집]",
        "[우리 ",
    ]
    code, out = _cli(app_config, db_path, docs, dry_run=True, source="posts")
    # 글 하나: 플랫폼 문서(본문)와 카드 문서(R4c, 원고당 하나)
    assert code == 0 and any(line.startswith("[우리 글] 파일 2개 · 조각 2개") for line in out)
    code, out = _cli(app_config, db_path, docs, stats=True)
    assert any("[수집] 조각 1개" in line for line in out)


def test_cli_rejects_unknown_source(app_config, docs: Path, tmp_path: Path) -> None:
    with pytest.raises(IndexInputError):
        _cli(app_config, tmp_path / "x.duckdb", docs, source="blog")


def test_cli_dry_run_without_db_file_skips_db_sources(
    app_config, docs: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "none.duckdb"
    code, out = _cli(app_config, db_path, docs, dry_run=True)
    assert code == 0 and not db_path.exists()
    assert any("DB 파일이 없어 건너뜁니다(수집, 우리 글)" in line for line in out)


def test_cli_dry_run_reads_db_without_writing(
    app_config, docs: Path, tmp_path: Path, clock
) -> None:
    """dry-run은 DB를 읽기 전용으로 연다: 스키마·마이그레이션을 쓰지 않는다(R4 리뷰)."""
    db_path = tmp_path / "x.duckdb"
    _seed_file_db(db_path, clock)
    before = db_path.stat().st_mtime_ns, db_path.stat().st_size
    code, out = _cli(app_config, db_path, docs, dry_run=True)
    assert code == 0 and any(line.startswith("[수집] 파일 1개") for line in out)
    assert (db_path.stat().st_mtime_ns, db_path.stat().st_size) == before


def test_cli_exit_code_two_when_a_source_cannot_be_read(
    app_config, docs: Path, tmp_path: Path, clock
) -> None:
    db_path = tmp_path / "x.duckdb"
    _seed_file_db(db_path, clock)
    assert _cli(app_config, db_path, docs)[0] == 0
    empty = tmp_path / "빈-문서"
    empty.mkdir()
    code, out = _cli(app_config, db_path, empty)  # 저장된 문서 조각이 있는데 0개
    assert code == 2 and any(line.startswith("[문서] 건너뜀") for line in out)
    assert any(line.startswith("[수집]") for line in out)  # 다른 출처는 계속
