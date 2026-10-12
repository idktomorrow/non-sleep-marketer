"""배치 Q1 수집 품질: 텍스트 정리(5), 원문 본문(1), 제외 장르·관련성 지시(2)."""

from __future__ import annotations

from datetime import UTC, datetime

from nsm.adapters.sources.common import Pacer, clean_text
from nsm.adapters.sources.web import BODY_MAX, NaverSearchSource, body_url, fetch_article
from nsm.domain.models import Classification, SourceItem
from nsm.engine.collect import relevance_instruction, run_collect
from nsm.engine.context import JobContext
from nsm.store.repo import Repository
from tests.fakes import FakeClassifier, FakeNotifier

# 2026-10-12에 실제로 수집된 요약문(화면 낭독기 문구가 본문에 섞임)
REAL_SNIPPET = (
    "가정용 전동 커피 그라인더 원두 분쇄기 고르는 기준 5가지 새 창 열림 핸드드립을 시작하고 나면 "
    "원두만큼 고민되는 게 바로 가정용 전동 커피 그라인더예요. 새 창 열림"
)


# ---------------------------------------------------------------- 5. 텍스트 정리
def test_clean_text_removes_a11y_phrases() -> None:
    out = clean_text(REAL_SNIPPET)
    assert "새 창 열림" not in out and "그라인더예요." in out


def test_clean_text_drops_menu_lines_keeps_sentences() -> None:
    page = "로그인\n메뉴\n본문 바로가기\n이웃추가\nURL 복사\n공유하기\n로그인 회원가입 메뉴\n\n물 온도는 90~96도가 기준이에요.\n댓글 3\n"
    out = clean_text(page)
    assert out.startswith("물 온도는 90~96도가 기준이에요.")
    for noise in ("로그인", "이웃추가", "URL 복사", "공유하기", "바로가기"):
        assert noise not in out


def test_clean_text_caps_length() -> None:
    assert len(clean_text("가" * 5000, 3000)) == 3000


# ---------------------------------------------------------------- 1. 원문 본문
def test_naver_blog_body_uses_mobile_url() -> None:
    assert body_url("https://blog.naver.com/coffee/2231") == "https://m.blog.naver.com/coffee/2231"
    assert (
        body_url("https://n.news.naver.com/mnews/article/001/1")
        == "https://n.news.naver.com/mnews/article/001/1"
    )


class ArticleDriver:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text, self.fail = text, fail
        self.visited: list[str] = []

    def goto(self, url: str) -> None:
        if self.fail:
            raise RuntimeError("timeout")
        self.visited.append(url)

    def article_text(self) -> str:
        return self.text

    def page_text(self) -> str:
        return "page " + self.text

    def links(self):
        return []

    def close(self) -> None:
        pass


def _pacer() -> Pacer:
    return Pacer(0, 0, sleep=lambda _s: None)


def test_fetch_article_cleans_caps_and_rejects_short() -> None:
    long = "로그인\n" + "핸드드립 물 온도는 추출 수율에 영향을 줍니다. " * 300
    d = ArticleDriver(long)
    body = fetch_article(d, _pacer(), "https://blog.naver.com/a/1")
    assert body and len(body) <= BODY_MAX and not body.startswith("로그인")
    assert d.visited == ["https://m.blog.naver.com/a/1"]
    assert fetch_article(ArticleDriver("짧은 글"), _pacer(), "https://x/1") is None
    assert fetch_article(ArticleDriver(fail=True), _pacer(), "https://x/1") is None


def test_naver_source_exposes_fetch_body(clock) -> None:
    src = NaverSearchSource(ArticleDriver("본문 " * 100), clock, _pacer())
    assert src.fetch_body("https://blog.naver.com/a/1")


# ---------------------------------------------------------------- 2. 관련성 지시
def test_relevance_instruction_says_informative_only(app_config) -> None:
    ctx = JobContext(config=app_config, repo=None, clock=None, notifier=None)  # type: ignore[arg-type]
    text = relevance_instruction(ctx)
    for must in ("정보성 글", "방문 후기", "여행기", "일기", "irrelevant"):
        assert must in text


# ---------------------------------------------------------------- collect 잡 전체
class RecordingClassifier(FakeClassifier):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.seen: list[str] = []

    def classify(self, text, labels, instruction):
        self.seen.append(text)
        return super().classify(text, labels, instruction)

    def classify_many(self, texts, labels, instruction):
        return [self.classify(t, labels, instruction) for t in texts]


class BodySource:
    name = "naver"

    def __init__(self, items: list[SourceItem], body: str | None) -> None:
        self.items, self.body = items, body
        self.fetched: list[str] = []

    def collect(self, topics, limit):
        return self.items

    def fetch_body(self, url: str) -> str | None:
        self.fetched.append(url)
        return self.body


def _item(i: int, title: str, url: str) -> SourceItem:
    return SourceItem(
        id=f"s{i}",
        source="naver",
        url=url,
        title=title,
        text=f"{title} 요약문",
        collected_at=datetime(2026, 10, 11, tzinfo=UTC),
        url_hash=f"h{i}",
    )


def _ctx(db, clock, app_config, source, relevance: FakeClassifier) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[source],
        relevance_classifier=relevance,
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
    )


def test_genre_rule_skips_classifier_and_body_fetch_only_for_relevant(
    db, clock, app_config
) -> None:
    items = [
        _item(1, "가정용 전동 커피 그라인더 고르는 기준 5가지", "https://blog.naver.com/a/1"),
        _item(2, "후쿠오카 카페 핸드드립 내돈내산 후기 ♥", "https://blog.naver.com/b/2"),
        _item(3, "코스피 마감 시황 정리", "https://n.news.naver.com/x/article/3"),
    ]
    src = BodySource(items, body="원문 본문입니다. " * 50)
    rel = RecordingClassifier(
        by_text={
            f"{items[2].title}\n{items[2].text}": Classification(
                label="irrelevant", confidence=0.95
            )
        },
        default=Classification(label="relevant", confidence=0.9),
    )
    ctx = _ctx(db, clock, app_config, src, rel)
    r = run_collect(ctx)
    assert r.counts["excluded_genre"] == 1 and r.counts["relevant"] == 1 and r.counts["bodies"] == 1
    # 후기 글은 분류기에 가지 않았다
    assert not any("내돈내산" in x for x in rel.seen)
    assert src.fetched == ["https://blog.naver.com/a/1"]  # 관련 자료만 원문을 연다
    got = {i.id: i for i, _, _ in ctx.repo.unused_relevant_items(0.8)}
    assert got["s1"].text.startswith("원문 본문입니다.")
    # 두 번째 실행에서는 다시 열지 않는다
    run_collect(ctx)
    assert src.fetched == ["https://blog.naver.com/a/1"]


def test_body_fetch_failure_keeps_snippet(db, clock, app_config) -> None:
    items = [_item(1, "핸드드립 물 온도 기준 정리", "https://blog.naver.com/a/1")]
    src = BodySource(items, body=None)
    ctx = _ctx(
        db,
        clock,
        app_config,
        src,
        FakeClassifier(default=Classification(label="relevant", confidence=0.9)),
    )
    r = run_collect(ctx)
    assert r.counts["bodies"] == 0
    ((item, _, _),) = ctx.repo.unused_relevant_items(0.8)
    assert item.text == "핸드드립 물 온도 기준 정리 요약문"


def test_clean_text_removes_zero_width_chars() -> None:
    # 2026-10-13 실측: 네이버 블로그 본문에 ​가 섞여 있었다
    assert clean_text("기준 5가지 ​ ​ 분쇄 방식﻿") == "기준 5가지 분쇄 방식"


def test_body_fetch_retries_failures_and_waits_for_missing_source(db, clock, app_config) -> None:
    items = [_item(1, "핸드드립 물 온도 기준 정리", "https://blog.naver.com/a/1")]
    src = BodySource(items, body=None)
    ctx = _ctx(
        db,
        clock,
        app_config,
        src,
        FakeClassifier(default=Classification(label="relevant", confidence=0.9)),
    )
    for _ in range(5):
        run_collect(ctx)
    assert len(src.fetched) == Repository.BODY_RETRIES  # 실패는 3번까지만 다시
    # 그 소스가 없는 실행(브라우저 없음)에서는 기록하지 않고 넘어간다
    items2 = [_item(2, "원두 보관 기준 정리", "https://blog.naver.com/b/2")]
    ctx2 = _ctx(
        db,
        clock,
        app_config,
        BodySource(items2, body=None),
        FakeClassifier(default=Classification(label="relevant", confidence=0.9)),
    )
    ctx2.sources = []
    from nsm.engine.collect import _fetch_bodies
    from nsm.engine.context import JobResult

    ctx2.repo.insert_source_item(items2[0])
    ctx2.repo.set_source_relevance("s2", 0.9)
    _fetch_bodies(ctx2, JobResult("collect"))
    assert [i.id for i in ctx2.repo.relevant_items_without_body(0.8, 10)] == ["s2"]
