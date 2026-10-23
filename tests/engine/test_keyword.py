"""키워드 원고(C15)와 수집 제외어: 사람이 넣은 키워드로 바로 찾아 원고 1건, 제외어가 든 글은 모으지도 쓰지도 않는다."""

from __future__ import annotations

import pytest

from nsm.domain.models import Classification, SourceItem
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import run_generate
from nsm.engine.keyword import KeywordError, clean_keyword, run_keyword_draft
from nsm.engine.runner import run_job
from nsm.engine.summary import describe
from nsm.store.repo import Repository
from tests.fakes import FakeClassifier, FakeNotifier, FakeTextGenerator


def _item(clock, n: int, title: str, source: str = "naver") -> SourceItem:
    return SourceItem(
        id=f"kw-{n}",
        source=source,
        url=f"https://example.com/kw-{n}",
        url_hash=f"kwh-{n}",
        title=title,
        text=f"{title} 정리. 출시 일정과 가격을 모았습니다.",
        collected_at=clock.now(),
    )


class KeywordSource:
    """받은 검색어마다 정해 둔 글을 돌려준다(없으면 빈 목록)."""

    def __init__(self, name: str, by_query: dict[str, list[SourceItem]]) -> None:
        self.name = name
        self.by_query = by_query
        self.calls: list[tuple[list[str], int]] = []

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        self.calls.append((list(topics), limit))
        return [i for t in topics for i in self.by_query.get(t, [])][:limit]


class CountingGenerator(FakeTextGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.briefs: list = []

    def generate_content(self, brief, persona, brand):
        self.briefs.append(brief)
        return super().generate_content(brief, persona, brand)


@pytest.fixture
def goods(clock) -> list[SourceItem]:
    return [
        _item(clock, 1, "치이카와 굿즈 신상 10월 입고 리스트"),
        _item(clock, 2, "치이카와 굿즈 품절 대란 정리"),
        _item(clock, 3, "극장판 치이카와 특전 굿즈 3주차"),
    ]


@pytest.fixture
def ctx(db, clock, app_config, goods) -> JobContext:
    naver = KeywordSource("naver", {"치이카와 굿즈": goods, "극장판 굿즈": goods[2:]})
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[naver, KeywordSource("google_trends", {}), KeywordSource("upload", {})],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9)
        ),
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
        text_generator=CountingGenerator(),
    )


def _with_exclude(ctx: JobContext, words: list[str]) -> None:
    s = ctx.config.settings
    collect = s.collect.model_copy(update={"exclude_words": words})
    ctx.config = ctx.config.model_copy(
        update={"settings": s.model_copy(update={"collect": collect})}
    )


def _used(ctx: JobContext) -> set[str]:
    rows = ctx.repo.db.execute(
        "SELECT id FROM source_items WHERE used_in_content_id IS NOT NULL "
        "AND used_in_content_id <> 'generation_failed'"
    )
    return {r[0] for r in rows}


def test_keyword_searches_now_and_makes_one_draft(ctx: JobContext) -> None:
    r = run_keyword_draft(ctx, "  치이카와   굿즈 ")
    assert r.ok and not r.skipped
    assert r.counts["keyword"] == "치이카와 굿즈"  # 공백 정리
    assert r.counts["made_info"] == 1 and r.counts["new"] == 3
    naver, trends, upload = ctx.sources
    assert naver.calls == [(["치이카와 굿즈"], 12)]
    assert trends.calls == [] and upload.calls == []  # 검색어를 받지 않는 소스는 부르지 않음
    # 관련 판정에 키워드도 관심 주제로 들어간다
    assert any("치이카와 굿즈" in c[2] for c in ctx.relevance_classifier.calls)
    brief = ctx.text_generator.briefs[0]
    assert brief.topic.startswith("치이카와 굿즈: ")
    assert {i.id for i in brief.sources} <= {"kw-1", "kw-2", "kw-3"}
    row = ctx.repo.db.execute("SELECT persona_topic, status FROM contents")[0]
    assert row == ("치이카와 굿즈", "pending_review")  # 보호 모드: 평소 규칙대로 검토
    assert ctx.notifier.calls[-1][0] == "키워드 원고"  # 추천 목록에 카드로


def test_keyword_uses_only_what_it_found_not_the_old_pool(ctx: JobContext, clock) -> None:
    old = [_item(clock, 9, "핸드드립 물 온도")]
    ctx.sources[0].by_query.update({t: old for t in ctx.config.persona.topics})
    run_collect(ctx)
    assert ctx.repo.unused_relevant_items(0.5)  # 예전 자료가 쌓여 있다
    run_keyword_draft(ctx, "치이카와 굿즈")
    assert "kw-9" not in _used(ctx) and _used(ctx) <= {"kw-1", "kw-2", "kw-3"}


def test_keyword_reuses_already_collected_unused_articles(ctx: JobContext, goods) -> None:
    for g in goods:
        ctx.repo.insert_source_item(g)
        ctx.repo.set_source_relevance(g.id, 0.9)
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    assert r.counts["new"] == 0 and r.counts["duplicate"] == 3 and r.counts["made_info"] == 1


def test_no_useful_material_makes_nothing_and_costs_nothing(ctx: JobContext) -> None:
    ctx.relevance_classifier = FakeClassifier(
        default=Classification(label="irrelevant", confidence=0.9)
    )
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    assert r.ok and "찾은 최근 글 3개 중 원고에 쓸 만한 자료가 없어" in r.skipped
    assert ctx.text_generator.briefs == [] and ctx.repo.count_contents() == 0
    nothing = run_keyword_draft(ctx, "없는 키워드")
    assert "찾은 최근 글이 없어 만들지 않음" in nothing.skipped


def test_keyword_respects_persona_switch_and_api_key(ctx: JobContext) -> None:
    ctx.config = ctx.config.model_copy(
        update={"persona": ctx.config.persona.model_copy(update={"enabled": False})}
    )
    assert run_keyword_draft(ctx, "치이카와 굿즈").skipped
    assert ctx.sources[0].calls == []  # 검색도 하지 않는다


def test_keyword_without_text_generator_is_skipped(ctx: JobContext) -> None:
    ctx.text_generator = None
    assert "API 키" in run_keyword_draft(ctx, "치이카와 굿즈").skipped


def test_exclude_words_drop_articles_but_keyword_can_unlock_them(ctx: JobContext) -> None:
    _with_exclude(ctx, ["극장판"])
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    assert r.counts["excluded_word"] == 1 and r.counts["new"] == 2
    assert ctx.repo.get_source_item("kw-3") is None  # 저장도 하지 않는다
    # 키워드에 제외어가 들어 있으면 이번에만 풀린다
    r2 = run_keyword_draft(ctx, "극장판 굿즈")
    assert r2.counts["new"] == 1 and "excluded_word" not in r2.counts


def test_clean_keyword_validates_length() -> None:
    assert clean_keyword(" 치이카와\n굿즈 ") == "치이카와 굿즈"
    for bad in ("", " ", "a", "가" * 41):
        with pytest.raises(KeywordError):
            clean_keyword(bad)


def test_runner_logs_keyword_runs_and_requires_a_value(ctx: JobContext) -> None:
    r = run_job("keyword", ctx, manual=True, arg="치이카와 굿즈")
    assert r.counts["made_info"] == 1
    assert ctx.repo.recent_runs(5)[0]["job"] == "keyword"
    with pytest.raises(ValueError, match="값이 필요"):
        run_job("keyword", ctx)


def test_keyword_summary_reads_as_a_sentence(ctx: JobContext) -> None:
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    text = describe("keyword", r.skipped, r.counts, r.errors)
    # S18: 키워드도 추천과 같은 흐름이라 추천 카드로 만든다(정보·밈 선택, 기본 정보)
    assert text.startswith("'치이카와 굿즈' 추천 1건 만듦(정보 1·밈 0)")
    assert "찾은 새 글 3개, 쓸 만한 자료 3개" in text


def test_collect_drops_excluded_and_generate_skips_old_excluded(ctx: JobContext, clock) -> None:
    movie, goods_item = _item(clock, 5, "극장판 관객 130만"), _item(clock, 6, "굿즈 입고 소식")
    ctx.sources[0].by_query.update({t: [movie, goods_item] for t in ctx.config.persona.topics})
    run_collect(ctx)  # 제외어 없을 때 둘 다 모음
    assert ctx.repo.get_source_item("kw-5") is not None
    _with_exclude(ctx, ["극장판"])
    late = _item(clock, 7, "극장판 특전 4주차")
    ctx.sources[0].by_query = {ctx.config.persona.topics[0]: [late]}
    r = run_collect(ctx)
    assert r.counts["excluded_word"] == 1 and ctx.repo.get_source_item("kw-7") is None
    assert "제외어가 든 글 1개 뺌" in describe("collect", "", r.counts)
    # 이미 모아 둔 극장판 자료는 원고에 쓰지 않는다
    run_generate(ctx, manual=True)
    assert "kw-5" not in _used(ctx) and "kw-6" in _used(ctx)


def test_keyword_scores_stay_in_the_run_and_do_not_leak(ctx: JobContext, clock) -> None:
    """키워드 기준 점수는 DB에 남기지 않는다: 안 쓴 글과 예전 미판정 자료는 평소 기준을 기다린다."""
    backlog = _item(clock, 8, "핸드드립 기초")
    ctx.repo.insert_source_item(backlog)  # 예전에 모았지만 아직 판정 전
    run_keyword_draft(ctx, "치이카와 굿즈")
    judged = [c[0] for c in ctx.relevance_classifier.calls]
    assert not any("핸드드립 기초" in t for t in judged)  # 키워드 기준으로 판정하지 않음
    rows = dict(ctx.repo.db.execute("SELECT id, relevance FROM source_items"))
    assert rows["kw-8"] is None
    unused = [i for i in ("kw-1", "kw-2", "kw-3") if i not in _used(ctx)]
    assert all(rows[i] is None for i in unused)  # 다음 자료 모으기가 평소 기준으로


def test_already_collected_low_score_article_is_rescored_for_keyword(
    ctx: JobContext, goods
) -> None:
    for g in goods:
        ctx.repo.insert_source_item(g)
        ctx.repo.set_source_relevance(g.id, 0.3)  # 평소 기준으로는 관련 없음
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    assert r.counts["made_info"] == 1
    assert dict(ctx.repo.db.execute("SELECT id, relevance FROM source_items"))["kw-1"] == 0.3


class BodySource(KeywordSource):
    def __init__(self, name, by_query, body: str) -> None:
        super().__init__(name, by_query)
        self.body = body
        self.fetched: list[str] = []

    def fetch_body(self, url: str) -> str:
        self.fetched.append(url)
        return self.body


def test_exclude_word_found_only_in_the_article_body_is_dropped(ctx: JobContext, goods) -> None:
    _with_exclude(ctx, ["극장판"])
    src = BodySource(
        "naver", {"치이카와 굿즈": goods[:2]}, "본문을 열어 보니 극장판 특전 이야기" * 5
    )
    ctx.sources = [src]
    r = run_keyword_draft(ctx, "치이카와 굿즈")
    assert src.fetched and "쓸 만한 자료가 없어" in r.skipped
    assert ctx.text_generator.briefs == []


def test_keyword_makes_picks_cards_through_the_same_flow(ctx: JobContext) -> None:
    """키워드도 오늘의 추천과 같은 흐름(S18): 결과는 추천 카드(계획 밖 슬롯), 종류 파싱."""
    from nsm.engine.keyword import parse_keyword_arg
    from nsm.store.picks import PicksStore

    assert parse_keyword_arg("both|치이카와") == (("info", "meme"), "치이카와")
    assert parse_keyword_arg("치이카와") == (("info",), "치이카와")  # 예전 형식
    r = run_keyword_draft(ctx, "info|치이카와 굿즈")
    assert r.ok and r.counts["made_info"] == 1
    picks = PicksStore(ctx.repo.db, ctx.clock).list()
    assert len(picks) == 1 and picks[0].kind == "info"
