"""C33 자잘한 수정: 끄는 중 중단 표시, Aside 오류 원인, 자료 정리, 해시태그 상한, 짧은 표기·예전 문구 제거."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.adapters.drivers import aside as A
from nsm.domain.labels import LEGACY_VIRTUAL_PERSON_TEXTS, VIRTUAL_PERSON_TEXT
from nsm.domain.models import ContentBrief, SourceItem
from nsm.domain.policy import apply_labels
from nsm.domain.quality import hashtag_count, rule_violations
from nsm.engine.context import JobContext
from nsm.engine.maintenance import run_maintenance
from nsm.engine.runner import INTERRUPTED_PREFIX, run_job
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakeTextGenerator


def _ctx(db, clock, app_config, **kw) -> JobContext:
    return JobContext(
        config=app_config, repo=Repository(db, clock), clock=clock, notifier=FakeNotifier(), **kw
    )


# ------------------------------------------------------------------ 2-1
def test_job_error_while_shutting_down_is_recorded_as_interrupted(
    db, clock, app_config, monkeypatch
) -> None:
    from nsm.engine import runner

    monkeypatch.setitem(
        runner.JOBS, "boom", lambda c: (_ for _ in ()).throw(RuntimeError("Aside 결과 줄이 없음"))
    )
    ctx = _ctx(db, clock, app_config)
    # 평소: 실패 + 알림
    run_job("boom", ctx)
    assert ctx.repo.recent_runs(1)[0]["ok"] is False
    assert not ctx.repo.recent_runs(1)[0]["message"].startswith(INTERRUPTED_PREFIX)
    assert ctx.notifier.calls and ctx.notifier.calls[-1][0].startswith("잡 실패")
    # 끄는 중: 중단으로 적고 알림 없음
    ctx.notifier.calls.clear()
    ctx.shutting_down = lambda: True
    run_job("boom", ctx)
    row = ctx.repo.recent_runs(1)[0]
    assert row["ok"] is False and row["message"].startswith(INTERRUPTED_PREFIX)
    assert not ctx.notifier.calls


def test_aside_missing_result_line_reports_exit_code_and_stderr() -> None:
    with pytest.raises(A.AsideError) as e:
        A.parse_output("", "Error: browser connection closed", 1)
    assert "종료 코드 1" in str(e.value) and "browser connection closed" in str(e.value)
    with pytest.raises(A.AsideError) as e2:
        A.parse_output("no marker here")
    assert "종료 코드 모름" in str(e2.value) and "no marker here" in str(e2.value)
    # stderr가 있어도 stdout 꼬리를 잃지 않는다
    with pytest.raises(A.AsideError) as e3:
        A.parse_output("page error: boom", "(node) ExperimentalWarning", 1)
    assert "ExperimentalWarning" in str(e3.value) and "page error: boom" in str(e3.value)
    # Ctrl+C 종료 코드는 "중단"으로 구분된다(프로그램이 끄는 중이라는 뜻)
    with pytest.raises(A.AsideError) as e4:
        A.parse_output("", "", 3221225786)
    assert e4.value.code == "interrupted"


def test_ctrl_c_in_adapter_marks_run_interrupted_even_before_shutdown_flag(
    db, clock, app_config, monkeypatch
) -> None:
    """리뷰: Ctrl+C는 CLI를 먼저 죽이므로 종료 플래그보다 예외가 먼저 온다. 예외 코드로도 중단을 안다."""
    from nsm.engine import runner
    from nsm.engine.context import JobResult

    def boom(c):
        raise A.AsideError("Aside CLI가 중단됨(Ctrl+C)", code="interrupted")

    monkeypatch.setitem(runner.JOBS, "boom", boom)
    ctx = _ctx(db, clock, app_config)
    run_job("boom", ctx)
    assert ctx.repo.recent_runs(1)[0]["message"].startswith(INTERRUPTED_PREFIX)
    assert not ctx.notifier.calls
    # 잡이 예외를 삼키고 errors에 적어도 result.note(e)로 중단이 전해진다
    r = JobResult("x", ok=False, errors=["threads 댓글 조회 실패"])
    r.note(A.AsideError("중단", code="interrupted"))
    assert r.interrupted


# ------------------------------------------------------------------ 2-4
def test_maintenance_blanks_then_deletes_stale_irrelevant_sources(
    db, clock, app_config, tmp_path
) -> None:
    ctx = _ctx(db, clock, app_config)
    now = clock.now()

    def item(i: str, days_ago: int) -> SourceItem:
        return SourceItem(
            id=i,
            source="naver",
            url=f"https://x/{i}",
            url_hash=f"h{i}",
            title=f"제목 {i}",
            text="본문 " * 50,
            collected_at=now - timedelta(days=days_ago),
        )

    for it in (
        item("fresh-irrel", 2),
        item("old-irrel", 10),
        item("ancient-irrel", 70),
        item("old-rel", 10),
        item("used", 70),
    ):
        ctx.repo.insert_source_item(it)
    for i, rel in (
        ("fresh-irrel", 0.1),
        ("old-irrel", 0.2),
        ("ancient-irrel", 0.0),
        ("old-rel", 0.95),
        ("used", 0.1),
    ):
        ctx.repo.set_source_relevance(i, rel)
        ctx.repo.record_classification("source_item", i, "relevance", "x", rel, "t")
    ctx.repo.mark_items_used(["used"], "content-1")
    ctx.repo.insert_source_item(item("never-judged", 30))  # 판정이 아직 없는 자료는 건드리지 않는다
    r = run_maintenance(ctx)
    assert r.counts["sources_blanked"] == 2 and r.counts["sources_pruned"] == 1
    texts = {r[0]: r[1] for r in ctx.repo.db.execute("SELECT id, text FROM source_items")}
    assert set(texts) == {
        "fresh-irrel",
        "old-irrel",
        "ancient-irrel",
        "old-rel",
        "used",
        "never-judged",
    }
    assert texts["old-irrel"] == "" and texts["ancient-irrel"] == ""
    assert texts["fresh-irrel"] != "" and texts["old-rel"] != "" and texts["never-judged"] != ""
    assert texts["used"] != ""  # 원고에 쓰인 자료는 본문도 행도 그대로
    # 행(지문)은 남아 같은 글을 다시 모으지 않는다. 60일 넘은 관련 없음은 판정 기록만 정리
    assert ctx.repo.has_url_hash("hancient-irrel") and ctx.repo.has_url_hash("hold-irrel")
    assert not ctx.repo.db.execute(
        "SELECT 1 FROM classifications WHERE target_id = 'ancient-irrel'"
    )
    assert ctx.repo.db.execute("SELECT 1 FROM classifications WHERE target_id = 'old-irrel'")
    assert "본문 비움" in r.summary() and "판정 기록" in r.summary()


# ------------------------------------------------------------------ 2-5
def test_hashtag_limit_counts_ad_label(app_config) -> None:
    src = SourceItem(
        id="s",
        source="naver",
        url="https://x",
        url_hash="h",
        title="t",
        text="x",
        collected_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    brief = ContentBrief(topic="t", template="info", sources=[src])
    gen = FakeTextGenerator(inject_text="#a #b #c #d #e #f").generate_content(
        brief, app_config.persona, app_config.brand
    )
    assert hashtag_count("#a #b c#d #e") == 3  # 영문 뒤 #은 해시태그가 아니다(C#, c#d)
    assert hashtag_count("#치이카와#먼작귀#팝업 굿즈") == 3  # 붙여 쓴 태그도 각각 센다
    assert any("해시태그" in v for v in rule_violations(gen, app_config.persona))
    four = FakeTextGenerator(inject_text="#a #b #c #d").generate_content(
        brief, app_config.persona, app_config.brand
    )
    assert not any("해시태그" in v for v in rule_violations(four, app_config.persona))
    five = FakeTextGenerator(inject_text="#a #b #c #d #e").generate_content(
        brief, app_config.persona, app_config.brand
    )
    plain = app_config.persona.model_copy(update={"ad_label": False})
    ad = app_config.persona.model_copy(update={"ad_label": True})
    assert not any("해시태그" in v for v in rule_violations(five, plain))
    assert any("상한 4개" in v for v in rule_violations(five, ad))  # 협찬이면 #광고 몫을 뺀다


# ------------------------------------------------------------------ 2-6
def test_short_label_and_legacy_long_label_is_stripped() -> None:
    assert VIRTUAL_PERSON_TEXT == "AI 가상인물 포함 게시물" and len(VIRTUAL_PERSON_TEXT) <= 15
    for legacy in LEGACY_VIRTUAL_PERSON_TEXTS:
        pasted = f"{legacy}\n\n본문"
        out = apply_labels(pasted, is_ad=False)
        assert (
            out.splitlines()[0] == VIRTUAL_PERSON_TEXT
            and legacy not in out
            and out.endswith("본문")
        )


def test_caption_hook_rule_is_in_prompt(app_config) -> None:
    from nsm.adapters.generator.claude_text import _system

    text = _system(app_config.persona, app_config.brand)
    assert "110자" in text and "해시태그 3~4개" in text
