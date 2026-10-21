"""keyword 잡: 사람이 넣은 키워드로 바로 자료를 찾아 원고 1건을 만든다 (배치 C15, ADR 0044).

자동 수집은 관심 주제 전체에 건수를 나눠 모으고 주제를 돌아가며 고르므로, "지금 이 이야기로"를
고를 수 없었다. 이 잡은 키워드 하나로 네이버 블로그·뉴스(와 켜 둔 커뮤니티)의 최근 글을 찾고,
관련 판정을 통과한 자료만으로 원고를 만든다.

- 관심 주제 목록은 바꾸지 않는다. 이번에 찾은 자료 중 남은 것은 평소처럼 주제 분류를 거쳐 자동 원고에 쓰인다.
- 자동 게시 여부는 평소 규칙(보호 모드·자동 게시 스위치·출처 검증)을 그대로 따른다.
- 쓸 만한 자료가 없으면 만들지 않는다(Claude 생성 비용도 들지 않는다).
- 수집 제외어는 지키되, 키워드 안에 든 제외어는 이번에만 풀어 준다("극장판"을 빼 두었어도 "극장판 굿즈"로 찾을 수 있게).
- 키워드 기준 판정 점수는 이번 실행에서만 쓰고 DB에 남기지 않는다. 안 쓴 글은 다음 자료 모으기가 평소 기준으로 판정한다.
"""

from __future__ import annotations

import logging

from nsm.domain.models import SourceItem
from nsm.domain.policy import local_day_bounds, should_act, under_cost_cap
from nsm.domain.quality import excluded_genre
from nsm.engine.collect import (
    RELEVANCE_LABELS,
    _fetch_bodies,
    excluded_word,
    relevance_instruction,
)
from nsm.engine.context import JobContext, JobResult
from nsm.engine.generate import (
    _RETRY_LATER,
    _SKIP_MATERIAL,
    SOURCES_PER_CONTENT,
    _make_one,
)

log = logging.getLogger(__name__)

KEYWORD_MIN, KEYWORD_MAX = 2, 40
SEARCH_LIMIT = 12  # 소스마다 찾을 글 수(블로그·뉴스 반씩)


class KeywordError(ValueError):
    """사람이 넣은 키워드가 형식에 맞지 않음. 화면에 그대로 보여 준다."""


def clean_keyword(raw: str) -> str:
    kw = " ".join(str(raw).split())
    if len(kw) < KEYWORD_MIN:
        raise KeywordError(f"키워드를 {KEYWORD_MIN}자 이상 넣어 주세요")
    if len(kw) > KEYWORD_MAX:
        raise KeywordError(f"키워드는 {KEYWORD_MAX}자까지입니다")
    return kw


def keyword_sources(ctx: JobContext) -> list:
    """검색어로 찾을 수 있는 소스만(구글 트렌드·올린 파일은 검색어를 받지 않는다). 구글 뉴스는 된다(C30)."""
    out = []
    for s in ctx.sources:
        name = str(getattr(s, "name", ""))
        if name in ("naver", "google_news") or name.startswith("community:"):
            out.append(s)
    return out


def run_keyword_draft(ctx: JobContext, keyword: str) -> JobResult:
    result = JobResult("keyword")
    keyword = clean_keyword(keyword)
    result.counts["keyword"] = keyword
    now = ctx.clock.now()
    ok, why = should_act(now, ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result
    s = ctx.config.settings
    if ctx.text_generator is None:
        result.skipped = "Claude API 키가 없어 생성할 수 없음 (`nsm init`)"
        return result
    start, end = local_day_bounds(now, ctx.tz)
    if not under_cost_cap(ctx.repo.cost_between(start, end), s.daily_cost_cap_usd):
        result.skipped = "오늘 비용 상한에 닿아 만들지 않음"
        return result
    sources = keyword_sources(ctx)
    if not sources:
        result.skipped = "키워드로 찾을 소스가 없음(설정의 네이버·구글 뉴스 수집이 꺼져 있음)"
        return result

    hashes = _search(ctx, sources, keyword, result)
    words = _active_exclude_words(ctx, keyword)
    found = [
        item
        for item, _, used in ctx.repo.items_by_url_hashes(hashes)
        if not used and not excluded_word(item, words) and not excluded_genre(item.title, item.text)
    ]
    candidates = _score_for_keyword(ctx, found, keyword, result)
    result.counts["relevant"] = len(candidates)
    if not candidates:
        result.skipped = _nothing_text(keyword, len(hashes))
        return result

    chosen = sorted(candidates, key=lambda x: -x[1])[:SOURCES_PER_CONTENT]
    result.counts["bodies"] = _fetch_bodies(ctx, result, items=[i for i, _ in chosen])
    # 원문을 읽었으면 요약문 대신 원문으로 만든다. 원문에만 제외어가 있던 글은 여기서 뺀다
    chosen = [(ctx.repo.get_source_item(i.id) or i, rel) for i, rel in chosen]
    chosen = [(i, rel) for i, rel in chosen if not excluded_word(i, words)]
    if not chosen:
        result.skipped = _nothing_text(keyword, len(hashes))
        return result

    made_today = ctx.repo.count_contents_between(start, end)
    cid = _make_one(ctx, (keyword, chosen), made_today, now, result, manual=True)
    if cid in (_RETRY_LATER, _SKIP_MATERIAL):
        result.ok = False
        return result
    content = ctx.repo.get_content(cid)
    pending = content is not None and content.status == "pending_review"
    result.counts.update(created=1, pending_review=int(pending), approved=int(not pending))
    if pending:
        ctx.notifier.notify("검토 대기 콘텐츠", f"'{keyword}' 원고가 검토를 기다립니다", "info")
    return result


def _nothing_text(keyword: str, found: int) -> str:
    if found:
        return f"'{keyword}'(으)로 찾은 최근 글 {found}개 중 원고에 쓸 만한 자료가 없어 만들지 않음"
    return f"'{keyword}'(으)로 찾은 최근 글이 없어 만들지 않음"


def _score_for_keyword(
    ctx: JobContext, items: list[SourceItem], keyword: str, result: JobResult
) -> list[tuple[SourceItem, float]]:
    """이번에 찾은 글을 키워드 기준으로 판정한 점수(이번 실행에서만 쓴다).

    DB의 관련성에는 쓰지 않는다. 키워드를 관심 주제로 더한 점수가 남으면, 안 쓴 글이 나중에
    자동 원고에 섞이고 예전 자료도 키워드로 판정되기 때문이다(코드 리뷰 C15). 새로 찾은 글은
    판정 전 상태로 남아 다음 자료 모으기가 평소 기준으로 판정한다.
    """
    if not items:
        return []
    if ctx.relevance_classifier is None:
        result.errors.append("분류기가 설정되지 않았습니다")
        return []
    texts = [f"{i.title}\n{i.text}" for i in items]
    instruction = relevance_instruction(ctx, keyword)
    try:
        rel = list(ctx.relevance_classifier.classify_many(texts, RELEVANCE_LABELS, instruction))
    except Exception as e:  # noqa: BLE001  판정이 안 되면 만들지 않는다(다시 누르면 된다)
        result.errors.append(f"관련성 분류 실패: {e}")
        return []
    threshold = ctx.config.settings.relevance_threshold
    out = []
    for item, c in zip(items, rel, strict=True):
        score = c.confidence if c.label == "relevant" else 1.0 - c.confidence
        if score >= threshold:
            out.append((item, score))
    return out


def _active_exclude_words(ctx: JobContext, keyword: str) -> list[str]:
    kw = keyword.lower()
    return [w for w in ctx.config.settings.collect.exclude_words if w.lower() not in kw]


def _search(ctx: JobContext, sources: list, keyword: str, result: JobResult) -> list[str]:
    """찾은 글의 URL 해시(이미 모아 둔 글도 포함). 제외어가 든 글은 저장하지 않는다."""
    words = _active_exclude_words(ctx, keyword)
    hashes: list[str] = []
    new = dup = excluded = 0
    for source in sources:
        try:
            items: list[SourceItem] = source.collect([keyword], SEARCH_LIMIT)
        except Exception as e:  # noqa: BLE001  소스 하나가 실패해도 나머지는 계속
            log.warning("키워드 검색 실패(%s): %s", getattr(source, "name", source), e)
            result.errors.append(f"{getattr(source, 'name', source)}: {e}")
            continue
        for item in items:
            if excluded_word(item, words):
                excluded += 1
                continue
            hashes.append(item.url_hash)
            if ctx.repo.insert_source_item(item):
                new += 1
            else:
                dup += 1
    result.counts.update(new=new, duplicate=dup)
    if excluded:
        result.counts["excluded_word"] = excluded
    return list(dict.fromkeys(hashes))
