"""C36: 미래 일정을 지난 일로 쓴 원고를 잡는다(ADR 0061). 고치지 않고 검토 대기로 보낸다.

연도 없는 날짜("1월 3일")는 오늘과 같은 해로 본다. 그래서 12월 30일에 쓴 "1월 3일에 열렸어요"(내년 일정)는
지난 날짜로 읽혀 잡히지 않는다. 이 한계는 ADR 0061에 적었고, 일부러 테스트하지 않는다.
C42: 같은 문장 앞에 연도 단서("2025년", 작년·올해·내년)가 있으면 뒤의 연도 없는 날짜가 그 해를 이어받는다(개정 1).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from nsm.adapters.generator.claude_text import _system
from nsm.domain import quality as Q
from nsm.engine.generate import run_generate
from tests.engine.test_generate_quality import ScriptedGenerator, _ctx, _first
from tests.unit.test_quality import _gen

NOV4 = date(2026, 11, 4)
KST = ZoneInfo("Asia/Seoul")


def _tense(app_config, text: str, today: date = NOV4, where: str = "caption") -> list[str]:
    gen = _gen(
        app_config, text if where == "caption" else "본문", text if where == "threads" else "본문"
    )
    return [
        v
        for v in Q.rule_violations(gen, app_config.persona, today)
        if v.startswith(Q.FUTURE_AS_PAST)
    ]


def test_future_date_in_past_tense_is_caught(app_config) -> None:  # (a)
    v = _tense(app_config, "2026년 11월 6일(금) 11시에 신상이 발매됐어요. 기대돼요")
    assert v == ["미래 일정을 지난 일로 씀: 2026년 11월 6일(금) 11시에 신상이 발매됐어요."]
    assert _tense(app_config, "11월 6일에 발매됐어요", where="threads")


def test_future_date_in_future_tense_passes(app_config) -> None:  # (b)
    assert _tense(app_config, "11월 6일에 발매돼요") == []
    assert _tense(app_config, "11월 6일 11시에 발매 예정이에요") == []


def test_past_date_in_past_tense_passes(app_config) -> None:  # (c)
    assert _tense(app_config, "9월 30일에 개봉했어요") == []
    assert _tense(app_config, "11월 4일에 시작됐어요") == []  # 오늘은 '뒤'가 아니다


def test_year_rollover_future_tense_passes(app_config) -> None:  # (d)
    assert _tense(app_config, "1월 3일에 열려요", today=date(2026, 12, 30)) == []


def test_dash_dates_and_card_text_are_checked(app_config) -> None:
    gen = _gen(app_config, "본문", "본문")
    slides = list(gen.deck.slides)
    i = next(n for n, s in enumerate(slides) if s.kind in ("body", "list"))
    slides[i] = slides[i].model_copy(update={"heading": "2026-11-20 팝업이 열렸어요"})
    gen = gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})
    v = Q.rule_violations(gen, app_config.persona, NOV4)
    assert any("2026-11-20 팝업이 열렸어요" in x for x in v)


def test_no_today_means_no_tense_check(app_config) -> None:
    gen = _gen(app_config, "11월 6일에 발매됐어요", "본문")
    assert not any(
        v.startswith(Q.FUTURE_AS_PAST) for v in Q.rule_violations(gen, app_config.persona)
    )


def test_invalid_date_is_skipped(app_config) -> None:
    assert _tense(app_config, "2월 30일에 나왔어요") == []


def test_generate_sends_it_to_review_with_reason_and_keeps_text(db, clock, app_config) -> None:
    """오늘 날짜는 시계에서(고정 가능). 다시 만들어도 같으면 문장을 고치지 않고 검토 대기로."""
    clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=KST))
    bad = "2026년 11월 6일(금) 11시에 신상이 발매됐어요"
    gen = ScriptedGenerator([(bad, "짧은 글이에요")])
    ctx = _ctx(db, clock, app_config, gen)
    run_generate(ctx)
    c = _first(ctx)
    assert c.status == "pending_review"
    assert f"미래 일정을 지난 일로 씀: {bad}" in c.review_reason
    assert c.generated.instagram_caption == bad  # 자동으로 고치지 않는다


def test_generate_passes_future_tense(db, clock, app_config) -> None:
    clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=KST))
    gen = ScriptedGenerator([("11월 6일 11시에 신상이 발매돼요 ☕", "짧은 글이에요")])
    ctx = _ctx(db, clock, app_config, gen)
    run_generate(ctx)
    c = _first(ctx)
    assert "미래 일정" not in (c.review_reason or "")


def test_prompt_compares_dates_with_today_and_asks_in_question_form(app_config) -> None:
    s = _system(app_config.persona, app_config.brand)
    assert "'오늘 날짜'와 비교해 시제를 정합니다" in s and "11월 6일 11시에 발매돼요" in s
    assert "이미 지났으면 지난 일로" in s
    assert "날짜를 밝혀 지난 일로 씁니다" not in s  # 옛 규칙(미래도 과거형으로 쓰게 했다)
    assert '"어떤 캐릭터가 제일 끌리나요?"' in s and "'~좋아요?'보다 '~좋으세요?'" in s


# ------------------------------------------------------------------ 리뷰 반영


def test_emoji_hashtag_or_laugh_after_the_ending_is_still_caught(app_config) -> None:
    for text in (
        "11월 6일에 신상이 발매됐어요🎉",
        "11월 6일에 발매됐어요 #신상",
        "11월 6일에 나왔어요ㅎㅎ",
    ):
        assert _tense(app_config, text), text


def test_modifier_and_announcement_are_not_flagged(app_config) -> None:
    assert _tense(app_config, "11월 6일, 기다렸던 신상이 나와요") == []  # 가운데 '~던'은 꾸밈말
    assert (
        _tense(app_config, "11월 20일 팝업 일정이 공개됐어요") == []
    )  # 일정을 알린 것은 지난 일이 맞다
    assert _tense(app_config, "11월 6일 발매가 확정됐어요") == []


def test_long_sentence_reason_is_marked_as_cut(app_config) -> None:
    long = "11월 6일에 " + "아주 " * 60 + "발매됐어요"
    v = _tense(app_config, long)
    assert len(v) == 1 and v[0].endswith("…")


def test_generator_prompt_date_uses_the_same_clock(clock) -> None:
    """프롬프트의 '오늘 날짜'도 시제 검사와 같은 시계에서 온다."""
    from nsm.adapters.generator.claude_text import ClaudeTextGenerator

    clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=KST))
    g = ClaudeTextGenerator(object(), "m", clock=clock, use_fallbacks=False)
    assert g._today() == date(2026, 11, 4)


def test_early_january_limit_is_known(app_config) -> None:
    """한계(ADR 0061): 1월 초에 '지난 12월 28일에 열렸어요'는 올해 12월로 읽혀 검토로 간다(오탐 쪽).
    연도 없는 날짜는 같은 해로 본다는 결정을 바꾸지 않았음을 고정한다."""
    assert _tense(app_config, "12월 28일에 팝업이 열렸어요", today=date(2027, 1, 3))


# ------------------------------------------------------------------ C42: 연도 이어받기

NOV8 = date(2026, 11, 8)


def test_production_sentence_with_year_then_yearless_end_passes(app_config) -> None:  # (a)
    """운영 문장(11-08 19:08): 앞의 "2025년"을 뒤의 "12월 31일"이 이어받는다."""
    s = "지난해 연말 성수기(2025년 10월 15일~12월 31일)에 한정 메뉴로 큰 인기를 얻었어요"
    assert _tense(app_config, s, today=NOV8) == []


def test_year_carries_over_range_and_list(app_config) -> None:  # (b)
    assert _tense(app_config, "2025년 10월 15일~12월 31일에 열렸어요", today=NOV8) == []
    assert _tense(app_config, "2025년 10월 15일과 12월 31일에 열렸어요", today=NOV8) == []
    assert _tense(app_config, "2025년 10월 15일부터 12월 31일까지 열렸어요", today=NOV8) == []


def test_yearless_range_is_this_year(app_config) -> None:  # (c)
    assert _tense(app_config, "11월 20일~12월 5일에 열렸어요", today=NOV8)


def test_last_year_hint(app_config) -> None:  # (d)
    assert _tense(app_config, "작년 12월 24일에 나왔어요", today=NOV8) == []
    assert _tense(app_config, "지난해 12월 24일에 나왔어요", today=NOV8) == []
    assert _tense(app_config, "전년 12월 24일에 나왔어요", today=NOV8) == []


def test_next_year_hint_is_caught(app_config) -> None:  # (e)
    assert _tense(app_config, "내년 1월 10일에 나왔어요", today=NOV8)


def test_explicit_future_year_is_caught(app_config) -> None:  # (f)
    assert _tense(app_config, "2026년 12월 31일에 끝났어요", today=NOV8)


def test_year_changes_within_one_sentence(app_config) -> None:  # (g)
    s = "작년 12월 24일부터 올해 1월 10일까지 열렸어요"
    assert _tense(app_config, s, today=NOV8) == []
    assert Q._dates_in(s, NOV8) == [date(2025, 12, 24), date(2026, 1, 10)]


def test_standalone_year_sets_base_and_latest_date_decides(app_config) -> None:
    assert Q._dates_in("2025년 연말, 12월 31일", NOV8) == [date(2025, 12, 31)]
    # 가장 늦은 날짜가 오늘 뒤면 걸린다(앞 날짜가 지났어도)
    assert _tense(app_config, "2026년 10월 1일~12월 31일에 열렸어요", today=NOV8)
