"""R2 코드 리뷰 반영: 하루 한도 장부, 끄는 중 멈춤, 실행마다 초기화, 옛 연결 닫기, ask 오류·후보, 작업 상태 표."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nsm.engine.context import JobContext
from nsm.engine.runner import INTERRUPTED_PREFIX, run_job
from nsm.rag.budget import USED_KEY, add_used, plan_embedding, quota_day, used_today
from nsm.rag.gemini import EmbeddingError, GeminiEmbedder
from nsm.store.repo import Repository
from tests.fakes import FakeEmbedder, FakeNotifier


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a, **k):
        raise AssertionError("테스트에서 네트워크 호출 금지")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    for i in range(4):
        (root / "adr" / f"000{i + 1}-x.md").write_text(
            f"# {i}번\n\n## 결정\n\n본문 {i}.\n", "utf-8"
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


def test_daily_ledger_keeps_second_manual_run_within_todays_quota(
    ctx: JobContext, app_config
) -> None:
    """ "지금 실행"을 또 눌러도 같은 (태평양) 날 한도를 두 번 쓰지 않는다."""
    app_config.settings.rag.embed_daily_limit = 3
    first = run_job("index", ctx)
    assert first.counts["sent"] == 3 and used_today(ctx.repo, ctx.clock.now()) == 3
    ctx.repo.db.execute("UPDATE doc_chunks SET embedding = NULL")  # 다시 보낼 게 생겨도
    second = run_job("index", ctx)
    assert second.counts["sent"] == 0 and "하루 한도를 이미 씀" in second.counts["note"]


def test_quota_day_follows_pacific_midnight() -> None:
    # 한국 2026-10-30 15:59 = 태평양 10-29 23:59(서머타임), 16:00 = 10-30 00:00
    before = datetime(2026, 10, 30, 6, 59, tzinfo=UTC)
    after = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)
    assert quota_day(before) == "2026-10-29" and quota_day(after) == "2026-10-30"


def test_ledger_resets_on_new_quota_day(ctx: JobContext) -> None:
    now = ctx.clock.now()
    add_used(ctx.repo, now, 999)
    assert used_today(ctx.repo, now) == 999
    assert used_today(ctx.repo, now + timedelta(days=1)) == 0
    assert ctx.repo.get_setting(USED_KEY)["day"] == quota_day(now)


def test_plan_reasons(ctx: JobContext, app_config) -> None:
    now = ctx.clock.now()
    assert plan_embedding(app_config, ctx.repo, None, now, 900).note.startswith("Gemini 키 없음")
    p = plan_embedding(app_config, ctx.repo, ctx.embedder, now, 900)
    assert p.embedder is ctx.embedder and p.max_embed == 900
    add_used(ctx.repo, now, 950)
    assert plan_embedding(app_config, ctx.repo, ctx.embedder, now, 900).max_embed == 50


def test_shutdown_stops_between_batches_and_logs_interrupted(ctx: JobContext) -> None:
    ctx.shutting_down = lambda: True
    res = run_job("index", ctx)
    assert res.interrupted and res.counts["sent"] == 0
    msg = next(r for r in ctx.repo.recent_runs(5) if r["job"] == "index")["message"]
    assert msg.startswith(INTERRUPTED_PREFIX)


def _ok(n: int, dim: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "embeddings": [{"values": [0.1] * dim} for _ in range(n)],
            "usageMetadata": {"promptTokenCount": 5},
        },
    )


def _client(responses: list[httpx.Response]) -> httpx.Client:
    it = iter(responses)
    return httpx.Client(transport=httpx.MockTransport(lambda req: next(it)))


def test_wait_between_batches_stops_when_shutting_down() -> None:
    sleeps: list[float] = []
    e = GeminiEmbedder(
        "k" * 30,
        "m",
        2,
        batch_size=1,
        per_minute=1,  # 한 건 보내면 1분 창이 차서 다음 건은 기다려야 한다
        client=_client([_ok(1, 2)]),
        sleep=sleeps.append,
        mono=lambda: 0.0,
    )
    stop = {"now": False}
    e.should_stop = lambda: stop["now"]
    e.embed(["a\nb"])
    stop["now"] = True
    with pytest.raises(EmbeddingError) as exc:
        e.embed(["c\nd"])  # 두 번째 묶음 앞 기다림에서 바로 끊김
    assert exc.value.code == "interrupted" and sum(sleeps) == 0


def test_reset_counts_tokens_per_run_without_waiting() -> None:
    sleeps: list[float] = []
    e = GeminiEmbedder(
        "k" * 30, "m", 2, client=_client([_ok(1, 2), _ok(1, 2)]), sleep=sleeps.append
    )
    e.embed(["a\nb"])
    e.reset()  # 다음 날 실행
    e.embed(["c\nd"])
    assert sum(sleeps) == 0 and e.tokens == 5


def test_refresh_closes_previous_embedder_client(db, app_config) -> None:
    from contextlib import ExitStack

    from nsm.config.secrets import MemorySecretStore
    from nsm.runtime import build_context, refresh_secret_parts

    app_config.settings.collect.naver = False
    secrets = MemorySecretStore({"gemini_api_key": "fake-gemini-key-AAAAAAAAAAAAAAAAAAAA"})
    with ExitStack() as stack:
        ctx = build_context(app_config, db, stack, secrets=secrets)
        old = ctx.embedder
        assert old is not None
        refresh_secret_parts(ctx, secrets)
        assert ctx.embedder is not old and old.client.is_closed


def test_ask_reports_embedding_errors_and_shows_candidates_below_threshold(
    app_config, ctx: JobContext, tmp_path: Path
) -> None:
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

    class Broken(FakeEmbedder):
        def embed_query(self, question: str) -> list[float]:
            raise EmbeddingError("Gemini 임베딩 하루 한도", "rate_limited")

    out: list[str] = []
    code = run_ask(
        app_config,
        path,
        "보호 모드",
        secrets=object(),
        embedder_factory=lambda *a: Broken(),
        echo=out.append,
    )
    assert code == 1 and "질문을 임베딩하지 못했습니다(rate_limited)" in out[0]
    out.clear()
    app_config.settings.rag.min_score = 0.999
    run_ask(
        app_config,
        path,
        "본문 1",
        k=2,
        secrets=object(),
        embedder_factory=lambda *a: FakeEmbedder(),
        echo=out.append,
    )
    assert "기준값 아래 후보" in out[2] and out[3].startswith("1. [") and out[5].startswith("2. [")


def test_index_appears_in_run_status_table(ctx: JobContext) -> None:
    from nsm.app.run_view import ORDER

    assert "index" in ORDER
    run_job("index", ctx)
    assert ctx.repo.last_run_at("index") is not None
    assert ctx.repo.last_run_at("never") is None
