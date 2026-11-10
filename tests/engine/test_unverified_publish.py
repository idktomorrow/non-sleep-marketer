"""C43: "게시는 됐지만 글 주소를 찾지 못함"을 성공으로 적지 않는다(incidents/0015, ADR 0020 개정 1).

실제 스레드 어댑터에 가짜 실행기를 붙여, 확인 실패 → 엔진 재시도(15분 뒤) → 재시도의 멱등 확인이 글을 찾아
published로 끝나는 흐름을 본다. 과거 줄 정리는 유지보수 잡이 한 번만 한다.
"""

from __future__ import annotations

from datetime import timedelta

from nsm.adapters.platforms.threads import ThreadsAdapter
from nsm.engine.context import JobContext
from nsm.engine.maintenance import run_maintenance
from nsm.engine.publish import recover_interrupted, run_publish
from nsm.store.db import to_db
from tests.engine.test_publish_job import ctx  # noqa: F401  같은 게시 시나리오 준비(원고 2개 승인)


class SeqRepl:
    """부를 때마다 다음 결과를 돌려주는 가짜 Aside 실행기."""

    def __init__(self, *results: dict) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        self.calls.append(args or {})
        return self.results.pop(0)


def _live_threads(ctx: JobContext, *results: dict) -> ThreadsAdapter:  # noqa: F811
    a = ThreadsAdapter(
        "@nonsleepmarketer", ctx.clock, repl=SeqRepl(*results), media_dir=ctx.media_dir
    )
    ctx.adapters["threads"] = a
    return a


def test_unverified_publish_retries_and_existing_post_ends_published(ctx: JobContext) -> None:  # noqa: F811
    a = _live_threads(
        ctx,
        {"code": "", "posted": True, "page": {"url": "https://www.threads.com/@nonsleepmarketer"}},
        {"code": "FOUND1", "existing": True},
    )
    pub_id = ctx.repo.due_publications("threads", ctx.clock.now())[0].id
    r = run_publish(ctx)
    p = ctx.repo.get_publication(pub_id)
    assert p.status == "failed" and p.attempts == 1
    assert p.next_attempt_at == ctx.clock.now() + timedelta(minutes=15)
    assert "게시를 확인하지 못함" in (p.last_error or "") and "(증거: " in p.last_error
    assert any("게시를 확인하지 못함" in e for e in r.errors)
    assert a.repl.calls[0]["probe"]  # 고유 문구가 있어 재시도의 멱등 확인이 가능하다

    ctx.clock.advance(minutes=15)
    run_publish(ctx)
    p = ctx.repo.get_publication(pub_id)
    assert p.status == "published" and p.external_id == "FOUND1"
    assert p.url == "https://www.threads.com/@nonsleepmarketer/post/FOUND1"
    assert len(a.repl.calls) == 2  # 두 번째는 게시 전 멱등 확인에서 찾고 다시 올리지 않음(existing)


def _set_pub(ctx: JobContext, pub_id: str, external_id: str | None) -> None:  # noqa: F811
    ctx.repo.db.execute(
        "UPDATE publications SET status = 'published', external_id = ?, url = 'u', "
        "published_at = ? WHERE id = ?",
        [external_id, to_db(ctx.clock.now()), pub_id],
    )


def test_maintenance_abandons_old_unverified_published_rows_once(ctx: JobContext) -> None:  # noqa: F811
    pubs = [ctx.repo.due_publications(p, ctx.clock.now())[0] for p in ("threads", "instagram")]
    _set_pub(ctx, pubs[0].id, "")  # C43 이전: 주소 못 찾음인데 성공으로 적힌 줄
    _set_pub(ctx, pubs[1].id, "Cabc")  # 코드가 있는 줄은 그대로
    content_before = ctx.repo.get_content(pubs[0].content_id).status
    ctx.clock.advance(minutes=10)
    recover_interrupted(ctx)  # 0.4.29를 켬: C43 기준 시각이 적힌다

    r = run_maintenance(ctx)
    assert r.counts["unverified_pubs"] == 1
    bad, good = (ctx.repo.get_publication(p.id) for p in pubs)
    assert bad.status == "abandoned"
    assert bad.last_error == "게시 확인 안 됨(C43 이전: 프로필에 글 없음)"
    assert good.status == "published" and good.external_id == "Cabc"
    assert ctx.repo.get_content(pubs[0].content_id).status == content_before  # 원고는 그대로
    assert "확인 안 된 게시 1줄 정리" in r.summary()

    # 한 번만: C43 뒤 짧은 글(고유 문구 없음)은 주소 없이 published가 정상이라 다음 정리에서 건드리지 않는다
    _set_pub(ctx, pubs[0].id, "")
    r2 = run_maintenance(ctx)
    assert "unverified_pubs" not in r2.counts
    assert ctx.repo.get_publication(pubs[0].id).status == "published"


def test_short_post_after_c43_start_is_kept_by_first_cleanup(ctx: JobContext) -> None:  # noqa: F811
    """0.4.29를 켠 뒤 첫 정리 전에 올라간 짧은 글(주소 없음)은 정상 기록이라 포기로 바꾸지 않는다(리뷰 반영)."""
    old, short = (
        ctx.repo.due_publications(p, ctx.clock.now())[0] for p in ("threads", "instagram")
    )
    _set_pub(ctx, old.id, "")
    ctx.clock.advance(minutes=10)
    recover_interrupted(ctx)  # 기준 시각
    recover_interrupted(ctx)  # 다시 켜도 기준 시각은 처음 것 그대로
    ctx.clock.advance(hours=2)
    _set_pub(ctx, short.id, "")  # C43 코드가 적은 짧은 글
    r = run_maintenance(ctx)
    assert r.counts["unverified_pubs"] == 1
    assert ctx.repo.get_publication(old.id).status == "abandoned"
    assert ctx.repo.get_publication(short.id).status == "published"
