"""원고 품질 하한(C31, ADR 0051): 빈약한 자료 제외, 구체 사실 판정, 같은 소식 제외, 주제 주간 상한,
비교형 조건, 구체 사실 하한, 일반론 문구."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nsm.domain.models import Classification, ContentBrief, SourceItem
from nsm.domain.quality import fact_count, rule_violations, same_event, title_tokens
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY, run_generate
from nsm.engine.material import DUPLICATE_EVENT
from nsm.store.repo import Repository
from tests.fakes import FakeClassifier, FakeNotifier, FakeSource, FakeTextGenerator

LONG = "치이카와 극장판이 10월 21일 기준 200만 관객을 넘었고 CGV 4DX 상영이 확정됐다. " * 20


def _item(id_: str, title: str, text: str = LONG) -> SourceItem:
    return SourceItem(
        id=id_,
        source="naver",
        url=f"https://example.com/{id_}",
        url_hash=f"h-{id_}",
        title=title,
        text=text,
        collected_at=datetime(2026, 10, 22, 0, 0, tzinfo=UTC),
    )


def _settings(app_config, **over):
    return app_config.model_copy(update={"settings": app_config.settings.model_copy(update=over)})


def _ctx(db, clock, cfg, items, topic="핸드드립", by_instruction=None, by_text=None) -> JobContext:
    c = JobContext(
        config=cfg,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", items)],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9),
            by_instruction=by_instruction,
        ),
        topic_classifier=FakeClassifier(
            default=Classification(label=topic, confidence=0.8), by_text=by_text
        ),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
    )
    c.repo.set_setting(AUTO_PUBLISH_KEY, True)
    return c


def _gen(app_config, inject: str):
    brief = ContentBrief(topic="t", template="info", sources=[_item("s1", "제목")])
    return FakeTextGenerator(inject_text=inject).generate_content(
        brief, app_config.persona, app_config.brand
    )


def _marker(ctx: JobContext, item_id: str) -> str | None:
    rows = ctx.repo.db.execute(
        "SELECT used_in_content_id FROM source_items WHERE id = ?", [item_id]
    )
    return rows[0][0] if rows else None


def _seed_content(
    ctx: JobContext, cid: str, persona_topic: str, template: str, at: datetime
) -> None:
    ctx.repo.db.execute(
        "INSERT INTO contents (id, status, topic, template, generated, relevance_confidence, is_ad, "
        "created_at, updated_at, persona_topic) VALUES (?, 'published', ?, ?, '{}', 1.0, false, ?, ?, ?)",
        [cid, f"{persona_topic}: 지난 글 {cid}", template, at, at, persona_topic],
    )


def test_fact_count_counts_numbers_dates_places_and_filler_is_a_violation(app_config) -> None:
    rich = _gen(app_config, "10월 25일 CGV 4DX에서 200만 돌파, 네이버쇼핑에서 검색해 보세요")
    assert fact_count(rich) >= 4  # 10월, 25일, 200만, CGV, 4DX …
    assert any("일반론" in v for v in rule_violations(rich, app_config.persona))
    thin = _gen(app_config, "좋은 하루 보내세요")
    assert fact_count(thin) < fact_count(rich)
    assert not any("일반론" in v for v in rule_violations(thin, app_config.persona))


def test_same_event_by_title_tokens() -> None:
    a = title_tokens("극장판 치이카와 200만 돌파, 4DX 상영 확정")
    b = title_tokens("'극장판 치이카와' 200만 관객 돌파…4DX 확정")
    c = title_tokens("치이카와 2027 달력 구매처 비교")
    assert same_event(a, b) and not same_event(a, c)
    assert not same_event(set(), a)


def test_thin_sources_are_not_used(db, clock, app_config) -> None:
    cfg = _settings(app_config, min_source_chars=800, protect_count=0)
    ctx = _ctx(db, clock, cfg, [_item("s1", "요약문만 있는 글", text="짧은 요약 247자 흉내")])
    run_collect(ctx)
    r = run_generate(ctx)
    assert r.counts.get("created", 0) == 0 and "쓸 자료 없음" in (r.skipped or "")
    ctx2 = _ctx(db, clock, cfg, [_item("s2", "본문이 긴 글")])
    run_collect(ctx2)
    assert run_generate(ctx2).counts["created"] == 1


def test_no_facts_items_are_judged_at_collect_and_excluded(db, clock, app_config) -> None:
    cfg = _settings(app_config, protect_count=0)
    ctx = _ctx(
        db,
        clock,
        cfg,
        [_item("s1", "일반론뿐인 글")],
        by_instruction={"구체적 사실": Classification(label="no_facts", confidence=0.9)},
    )
    r = run_collect(ctx)
    assert r.counts["facts_judged"] == 1 and r.counts["no_facts"] == 1
    assert run_generate(ctx).counts.get("created", 0) == 0
    rows = ctx.repo.db.execute(
        "SELECT label FROM classifications WHERE target_id = 's1' AND kind = 'facts'"
    )
    assert rows == [("no_facts",)]
    # 다시 수집해도 같은 자료를 두 번 묻지 않는다
    assert run_collect(ctx).counts["facts_judged"] == 0


def test_duplicate_event_is_skipped_by_title_then_by_model(db, clock, app_config) -> None:
    cfg = _settings(app_config, protect_count=0)
    ctx = _ctx(db, clock, cfg, [_item("s1", "극장판 치이카와 200만 돌파 4DX 상영 확정")])
    run_collect(ctx)
    assert run_generate(ctx).counts["created"] == 1
    # 제목이 거의 같은 새 자료 → 코드 단계에서 제외
    ctx2 = _ctx(db, clock, cfg, [_item("s2", "'극장판 치이카와' 200만 관객 돌파…4DX 확정")])
    run_collect(ctx2)
    assert run_generate(ctx2).counts.get("created", 0) == 0
    assert _marker(ctx2, "s2") == DUPLICATE_EVENT
    # 제목은 다르지만 분류기가 같은 소식이라 하면 → 모델 단계에서 제외
    ctx3 = _ctx(
        db,
        clock,
        cfg,
        [_item("s3", "인어 섬의 비밀, 관객 반응 뜨겁다")],
        by_instruction={"최근에 올린 게시물": Classification(label="same_news", confidence=0.9)},
    )
    run_collect(ctx3)
    assert run_generate(ctx3).counts.get("created", 0) == 0
    assert _marker(ctx3, "s3") == DUPLICATE_EVENT
    # 새 소식이면 쓴다
    ctx4 = _ctx(db, clock, cfg, [_item("s4", "치이카와 팝업스토어 11월 3일 더현대 서울 오픈")])
    run_collect(ctx4)
    assert run_generate(ctx4).counts["created"] == 1


def test_topic_weekly_limit_pushes_saturated_topic_back(db, clock, app_config) -> None:
    now = clock.now()
    by_text = {
        f"드립 소식\n{LONG}": Classification(label="핸드드립", confidence=0.9),
        f"원두 소식\n{LONG}": Classification(label="원두", confidence=0.9),
    }
    items = [_item("s1", "드립 소식"), _item("s2", "원두 소식")]

    def fresh(limit: int) -> JobContext:
        cfg = _settings(app_config, protect_count=0, topic_weekly_limit=limit)
        c = _ctx(db, clock, cfg, items, by_text=by_text)
        c.repo.db.execute("DELETE FROM contents")
        c.repo.db.execute("DELETE FROM source_items")
        # 이번 주 핸드드립 원고 2건(오래됨), 원두 1건(최근) → 오래 안 쓴 쪽은 핸드드립
        _seed_content(c, "a1", "핸드드립", "info", now - timedelta(days=3))
        _seed_content(c, "a2", "핸드드립", "list", now - timedelta(days=2))
        _seed_content(c, "b1", "원두", "info", now - timedelta(days=1))
        run_collect(c)
        return c

    c = fresh(limit=2)
    assert run_generate(c, manual=True).counts["created"] == 1
    assert c.repo.db.execute(
        "SELECT persona_topic FROM contents ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ) == [("원두",)]
    c = fresh(limit=0)  # 상한이 없으면 가장 오래 안 쓴 핸드드립
    assert run_generate(c, manual=True).counts["created"] == 1
    assert c.repo.db.execute(
        "SELECT persona_topic FROM contents ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ) == [("핸드드립",)]


def test_compare_template_needs_two_sources(db, clock, app_config) -> None:
    cfg = _settings(app_config, protect_count=0)
    ctx = _ctx(db, clock, cfg, [_item("s9", "혼자 남은 소식")])
    run_collect(ctx)
    now = clock.now()
    _seed_content(ctx, "c0", "원두", "info", now - timedelta(minutes=2))
    _seed_content(ctx, "c1", "원두", "list", now - timedelta(minutes=1))  # 다음 차례는 compare
    r = run_generate(ctx, manual=True)
    assert r.counts["templates"] == ["info"]  # 자료가 하나라 비교형 대신 정보형


def test_min_facts_sends_thin_draft_to_review(db, clock, app_config) -> None:
    cfg = _settings(app_config, protect_count=0, min_facts=100)
    ctx = _ctx(db, clock, cfg, [_item("s1", "소식")])
    run_collect(ctx)
    r = run_generate(ctx)
    assert r.counts["created"] == 1 and r.counts["pending_review"] == 1 and r.counts["thin"] == 1
    c = ctx.repo.list_contents()[0]
    assert c.status == "pending_review" and "구체 사실 부족" in (c.review_reason or "")


def test_max_bodies_per_run_setting_limits_fetching(db, clock, app_config) -> None:
    class BodySource(FakeSource):
        def __init__(self, items):
            super().__init__("naver", items)
            self.fetched: list[str] = []

        def fetch_body(self, url: str) -> str | None:
            self.fetched.append(url)
            return LONG

    cfg = _settings(app_config, max_bodies_per_run=2)
    src = BodySource([_item(f"s{i}", f"소식 {i}", text="짧은 요약") for i in range(5)])
    ctx = _ctx(db, clock, cfg, [])
    ctx.sources = [src]
    r = run_collect(ctx)
    assert r.counts["bodies"] == 2 and len(src.fetched) == 2


def test_fact_regexes_handle_particles_dates_and_list_markers(app_config) -> None:
    # 리뷰: 한글 뒤 조사("다이소에서", "팝업스토어에"), 날짜 하나는 사실 하나, 글머리 "1."은 안 센다
    g = _gen(app_config, "다이소에서 팝업스토어에 2026-10-25 공개. 1. 먼저 2. 다음")
    assert fact_count(g) >= 3  # 다이소, 팝업스토어, 2026-10-25
    only_marker = _gen(app_config, "순위는 1. 끝")
    base = _gen(app_config, "끝")
    assert fact_count(only_marker) == fact_count(base)


def test_keyword_classifier_does_not_judge_facts(db, clock, app_config) -> None:
    from nsm.adapters.classifier.keyword import KeywordClassifier

    # 낱말 규칙의 관련성 점수는 0.7이라 기준을 낮춘다
    cfg = _settings(app_config, protect_count=0, relevance_threshold=0.6)
    ctx = _ctx(db, clock, cfg, [_item("s1", "소식")])
    ctx.relevance_classifier = KeywordClassifier({"relevant": ["소식"]}, default_label="irrelevant")
    ctx.classifier_name = "keyword"
    r = run_collect(ctx)
    assert r.counts["facts_judged"] == 0
    assert ctx.repo.db.execute("SELECT facts FROM source_items WHERE id = 's1'") == [(None,)]
    assert run_generate(ctx).counts["created"] == 1  # 비어 있으면 통과


def test_facts_are_rejudged_when_body_arrives_later(db, clock, app_config) -> None:
    class LateBody(FakeSource):
        def __init__(self, items):
            super().__init__("naver", items)
            self.ready = False

        def fetch_body(self, url: str) -> str | None:
            return LONG if self.ready else None

    cfg = _settings(app_config, protect_count=0)
    src = LateBody([_item("s1", "소식", text="짧은 요약")])
    judged: list[str] = []

    def by_text_judge(text: str) -> Classification:
        judged.append(text)
        return Classification(
            label="no_facts" if "짧은 요약" in text else "has_facts", confidence=0.9
        )

    ctx = _ctx(db, clock, cfg, [])
    ctx.sources = [src]
    ctx.relevance_classifier = type(
        "C",
        (),
        {
            "classify": lambda self, t, labels, inst: (
                Classification(label="relevant", confidence=0.9)
                if "has_facts" not in labels
                else by_text_judge(t)
            ),
            "classify_many": lambda self, ts, labels, inst: [
                self.classify(t, labels, inst) for t in ts
            ],
        },
    )()
    # 첫 실행: 본문 실패(재시도 남음) → 판정 보류
    assert run_collect(ctx).counts["facts_judged"] == 0
    # 재시도 소진 → 요약문으로 판정 → none
    for _ in range(3):
        run_collect(ctx)
    assert ctx.repo.db.execute("SELECT facts FROM source_items WHERE id = 's1'") == [("none",)]
    # 본문이 나중에 오면 다시 판정 → has
    src.ready = True
    ctx.repo.db.execute("DELETE FROM classifications WHERE kind = 'body'")
    run_collect(ctx)
    assert ctx.repo.db.execute("SELECT facts FROM source_items WHERE id = 's1'") == [("has",)]


def test_same_news_is_asked_in_one_batch_and_remembered(db, clock, app_config) -> None:
    cfg = _settings(app_config, protect_count=0)
    ctx = _ctx(db, clock, cfg, [_item("s0", "극장판 치이카와 200만 돌파 4DX 상영 확정")])
    run_collect(ctx)
    assert run_generate(ctx).counts["created"] == 1
    items = [_item(f"n{i}", f"전혀 다른 제목 {i} 번째 기사") for i in range(4)]
    ctx2 = _ctx(
        db,
        clock,
        cfg,
        items,
        by_instruction={"최근에 올린 게시물": Classification(label="same_news", confidence=0.9)},
    )
    run_collect(ctx2)
    before = len(ctx2.relevance_classifier.calls)
    assert run_generate(ctx2).counts.get("created", 0) == 0
    asked = [c for c in ctx2.relevance_classifier.calls[before:] if "최근에 올린" in c[2]]
    assert len(asked) == 4  # classify_many 한 번(가짜는 건마다 classify를 부른다) — 같은 지시
    rows = ctx2.repo.db.execute("SELECT count(*) FROM classifications WHERE kind = 'same_news'")
    assert rows == [(4,)]
    # 두 번째 실행은 기록을 보고 다시 묻지 않는다
    before2 = len(ctx2.relevance_classifier.calls)
    run_generate(ctx2)
    assert not [c for c in ctx2.relevance_classifier.calls[before2:] if "최근에 올린" in c[2]]


def test_recent_titles_are_newest_first(db, clock, app_config) -> None:
    ctx = _ctx(db, clock, app_config, [])
    now = clock.now()
    for i, name in enumerate(["ㄱ 오래된 글", "ㅎ 최근 글", "ㄴ 중간 글"]):
        _seed_content(ctx, f"t{i}", "핸드드립", "info", now - timedelta(days=[3, 1, 2][i]))
        ctx.repo.db.execute("UPDATE contents SET topic = ? WHERE id = ?", [name, f"t{i}"])
    assert ctx.repo.recent_titles(now - timedelta(days=14), 2) == ["ㅎ 최근 글", "ㄴ 중간 글"]
