"""원고 재료 고르기(C31, ADR 0051): 빈약한 자료·이미 올린 소식·한 소재 쏠림을 걸러 "쓸 만한 자료 묶음"을 낸다.

순서
1. 관련 자료 중 본문이 `min_source_chars`보다 짧거나(요약문만 남은 것) 구체 사실이 없다고 판정된 것은 뺀다.
2. 최근 `topic_cooldown_days` 동안 만든 원고의 제목·사실 주장과 **같은 사건**으로 보이는 자료는 뺀다.
   1차는 코드(제목 낱말 겹침, 페르소나 주제 낱말은 제외), 2차는 분류기(0원)에게 "같은 소식인가"를 한 묶음으로
   묻는다. 판정은 기록해 같은 자료를 다시 묻지 않고, 같은 소식이면 `used_in_content_id = duplicate_event`.
3. 주제(소재 묶음)마다 최근 7일 원고 수가 `topic_weekly_limit` 이상이면 뒤로 미룬다. 가장 오래 안 쓴 주제부터.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from nsm.domain.models import SourceItem
from nsm.domain.policy import local_day_bounds
from nsm.domain.quality import same_event, title_tokens
from nsm.engine.collect import _who, excluded_word
from nsm.engine.context import JobContext

log = logging.getLogger(__name__)

DUPLICATE_EVENT = "duplicate_event"  # source_items.used_in_content_id 표시값: 이미 올린 소식
SAME_NEWS_LABELS = ["same_news", "new_news"]
SAME_NEWS_KIND = "same_news"
RECENT_TITLES_MAX = 20
MODEL_BATCH = 10  # 한 주제에서 분류기에 한 번에 묻는 후보 수


def same_news_instruction(recent_titles: list[str]) -> str:
    listing = "\n".join(f"- {t}" for t in recent_titles[:RECENT_TITLES_MAX])
    return (
        "아래는 이 계정이 최근에 올린 게시물 제목이다. 주어진 자료가 이 중 하나와 같은 사건·같은 소식을 "
        "다루면 same_news, 다른 사건(새 행사·새 상품·새 날짜·다른 대상)을 다루면 new_news로 답하라. "
        "같은 영화·상품·행사의 흥행 성적(예매 순위, 관객 수, 매출, 품절 소식)은 숫자가 바뀌어도 같은 소식이다. "
        "같은 대상의 후속 보도·반응 기사도 같은 소식이다.\n"
        f"최근 게시물:\n{listing}"
    )


def _topic_stop(ctx: JobContext) -> set[str]:
    """페르소나 주제 낱말(어느 글에나 들어가 겹침을 부풀린다)은 제목 비교에서 뺀다."""
    return {t for topic in ctx.config.persona.topics for t in title_tokens(topic)}


def _recent_texts(
    ctx: JobContext, since: datetime, stop: set[str]
) -> tuple[list[str], list[set[str]]]:
    """최근 원고 제목(최신순)과, 제목+사실 주장의 낱말 집합 목록."""
    titles = ctx.repo.recent_titles(since, RECENT_TITLES_MAX)
    claims = ctx.repo.recent_claims(since)
    token_sets = [title_tokens(t, stop) for t in titles] + [title_tokens(c, stop) for c in claims]
    return titles, [s for s in token_sets if s]


def _model_duplicates(
    ctx: JobContext, candidates: list[SourceItem], titles: list[str], basis: str
) -> set[str]:
    """분류기에게 후보들을 한 묶음으로 묻고 같은 소식인 자료 id를 돌려준다.

    판정은 "누가|무엇과 비교했나(그때의 최신 원고 id)"와 함께 기록한다. 같은 소식이면 다시 묻지 않고,
    새 소식이면 기준(최신 원고)이 그대로일 때만 믿는다. 같은 실행 안에서 첫 원고가 생기면 기준이 바뀌어
    남은 후보를 다시 묻는다(C34 — 시각 비교는 원고 시각이 실행 시작 시각이라 믿을 수 없었다).
    """
    if not titles or ctx.relevance_classifier is None or not candidates:
        return set()
    dups: set[str] = set()
    ask: list[SourceItem] = []
    prev = ctx.repo.latest_classifications([i.id for i in candidates], SAME_NEWS_KIND)
    for item in candidates:
        found = prev.get(item.id)
        if found is None:
            ask.append(item)
        elif found[0] == "same_news":
            dups.add(item.id)
        elif found[1].rsplit("|", 1)[-1] != basis:
            ask.append(item)  # 그때와 최신 원고가 다르다: 새 원고와도 비교해야 한다
    if not ask:
        return dups
    texts = [f"{i.title}\n{i.text[:1500]}" for i in ask]
    try:
        labels = list(
            ctx.relevance_classifier.classify_many(
                texts, SAME_NEWS_LABELS, same_news_instruction(titles)
            )
        )
    except Exception as e:  # noqa: BLE001  판정 실패는 "새 소식"으로 보고 진행(원고가 멈추지 않게)
        log.warning("같은 소식 판정 실패(새 소식으로 간주): %s", e)
        return dups
    who = f"{_who(ctx, ctx.relevance_classifier)}|{basis}"
    for item, c in zip(ask, labels, strict=True):
        ctx.repo.record_classification(
            "source_item", item.id, SAME_NEWS_KIND, c.label, c.confidence, who
        )
        if c.label == "same_news":
            dups.add(item.id)
    return dups


def pick_material(
    ctx: JobContext, per_content: int = 3
) -> tuple[str, list[tuple[SourceItem, float]]] | None:
    """쓸 자료 묶음: (페르소나 주제, [(자료, 관련성)...]) 또는 None(쓸 자료 없음)."""
    s = ctx.config.settings
    now = ctx.clock.now()
    words = s.collect.exclude_words
    groups: dict[str, list[tuple[SourceItem, float]]] = defaultdict(list)
    thin = 0
    for item, rel, topic, facts in ctx.repo.unused_relevant_items(s.relevance_threshold):
        if not topic or excluded_word(item, words):  # 제외어를 나중에 넣었어도(C15)
            continue
        # 1. 요약문만 남은 자료·구체 사실이 없는 자료는 원고 재료가 못 된다(C31)
        if len(item.text) < s.min_source_chars or facts == "none":
            thin += 1
            continue
        groups[topic].append((item, rel))
    if not groups:
        if thin:
            log.info("관련 자료 %d건이 본문이 짧거나 구체 사실이 없어 원고 재료에서 제외", thin)
        return None

    # 2. 이미 올린 소식 거르기(1차 코드)
    stop = _topic_stop(ctx)
    since = now - timedelta(days=s.topic_cooldown_days)
    titles, recent_tokens = _recent_texts(ctx, since, stop)
    dup_ids: list[str] = []
    for topic in list(groups):
        kept = []
        for item, rel in groups[topic]:
            toks = title_tokens(item.title, stop)
            if any(same_event(toks, r) for r in recent_tokens):
                dup_ids.append(item.id)
            else:
                kept.append((item, rel))
        groups[topic] = kept
        if not kept:
            del groups[topic]
    if dup_ids:
        ctx.repo.mark_items_used(dup_ids, DUPLICATE_EVENT)
        log.info("최근 게시물과 같은 사건(제목 겹침)으로 자료 %d건 제외", len(dup_ids))
    if not groups:
        return None

    # 3. 주제 순서: 주간 상한 아래인 주제 먼저, 오늘 아직 안 쓴 주제 먼저(같은 주제 하루 두 번 방지, C34),
    #    그다음 가장 오래 안 쓴 주제
    last_used = ctx.repo.persona_topic_last_used()
    weekly = ctx.repo.persona_topic_counts_since(now - timedelta(days=7))
    day_start, _ = local_day_bounds(now, ctx.tz)
    today = ctx.repo.persona_topic_counts_since(day_start)
    epoch = datetime.min.replace(tzinfo=now.tzinfo)
    limit = s.topic_weekly_limit

    def order(t: str) -> tuple:
        over = bool(limit) and weekly.get(t, 0) >= limit
        return (over, t in today, last_used.get(t, epoch), -len(groups[t]))

    for topic in sorted(groups, key=order):
        if limit and weekly.get(topic, 0) >= limit:
            log.info(
                "주제 '%s'는 최근 7일 원고 %d건으로 주간 상한에 닿아 뒤로 미룸",
                topic,
                weekly[topic],
            )
        ranked = sorted(groups[topic], key=lambda x: -x[1])
        # 2차: 제목이 달라도 같은 소식이면(분류기, 상위 후보를 한 묶음으로) 뺀다
        basis = ctx.repo.latest_content_id() or "-"  # 원고가 생길 때마다 바뀐다(같은 실행 안 포함)
        dups = _model_duplicates(ctx, [i for i, _ in ranked[:MODEL_BATCH]], titles, basis)
        if dups:
            ctx.repo.mark_items_used(list(dups), DUPLICATE_EVENT)
            log.info("최근 게시물과 같은 소식(분류기)으로 자료 %d건 제외", len(dups))
        chosen = [(i, r) for i, r in ranked if i.id not in dups][:per_content]
        if chosen:
            return topic, chosen
    return None
