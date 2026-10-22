"""원고 재료 고르기(C31, ADR 0051): 빈약한 자료·이미 올린 소식·한 소재 쏠림을 걸러 "쓸 만한 자료 묶음"을 낸다.

순서
1. 관련 자료 중 본문이 `min_source_chars`보다 짧거나(요약문만 남은 것) 구체 사실이 없다고 판정된 것은 뺀다.
2. 최근 `topic_cooldown_days` 동안 만든 원고의 제목·사실 주장과 **같은 사건**으로 보이는 자료는 뺀다.
   1차는 코드(제목 낱말 겹침), 2차는 분류기(0원)에게 "같은 소식인가"를 묻는다. 같은 소식이면 두 번 묻지 않게
   `used_in_content_id = duplicate_event`로 표시한다.
3. 주제(소재 묶음)마다 최근 7일 원고 수가 `topic_weekly_limit` 이상이면 뒤로 미룬다. 가장 오래 안 쓴 주제부터.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from nsm.domain.models import SourceItem
from nsm.domain.quality import same_event, title_tokens
from nsm.engine.collect import excluded_word
from nsm.engine.context import JobContext

log = logging.getLogger(__name__)

DUPLICATE_EVENT = "duplicate_event"  # source_items.used_in_content_id 표시값: 이미 올린 소식
SAME_NEWS_LABELS = ["same_news", "new_news"]
RECENT_TITLES_MAX = 20


def same_news_instruction(recent_titles: list[str]) -> str:
    listing = "\n".join(f"- {t}" for t in recent_titles[:RECENT_TITLES_MAX])
    return (
        "아래는 이 계정이 최근에 올린 게시물 제목이다. 주어진 자료가 이 중 하나와 같은 사건·같은 소식을 "
        "다루면 same_news, 새로운 사실(새 행사·새 상품·새 날짜·다른 소식)을 담고 있으면 new_news로 답하라. "
        "같은 주제라도 새 사실이 있으면 new_news다.\n"
        f"최근 게시물:\n{listing}"
    )


def _recent_texts(ctx: JobContext, since: datetime) -> tuple[list[str], list[set[str]]]:
    """최근 원고의 제목 목록과, 제목+사실 주장 낱말 집합 목록."""
    titles = sorted(ctx.repo.recent_topics(since))
    claims = ctx.repo.recent_claims(since)
    token_sets = [title_tokens(t) for t in titles] + [title_tokens(c) for c in claims]
    return titles, [s for s in token_sets if s]


def _is_duplicate_by_code(item: SourceItem, recent: list[set[str]]) -> bool:
    toks = title_tokens(item.title)
    return any(same_event(toks, r) for r in recent)


def _is_duplicate_by_model(ctx: JobContext, item: SourceItem, titles: list[str]) -> bool:
    if not titles or ctx.relevance_classifier is None:
        return False
    try:
        c = ctx.relevance_classifier.classify(
            f"{item.title}\n{item.text[:1500]}", SAME_NEWS_LABELS, same_news_instruction(titles)
        )
    except Exception as e:  # noqa: BLE001  판정 실패는 "새 소식"으로 보고 진행(원고가 멈추지 않게)
        log.warning("같은 소식 판정 실패(새 소식으로 간주): %s", e)
        return False
    return c.label == "same_news"


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
    since = now - timedelta(days=s.topic_cooldown_days)
    titles, recent_tokens = _recent_texts(ctx, since)
    dup_ids: list[str] = []
    for topic in list(groups):
        kept = []
        for item, rel in groups[topic]:
            if _is_duplicate_by_code(item, recent_tokens):
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

    # 3. 주제 순서: 주간 상한 아래인 주제 먼저, 그다음 가장 오래 안 쓴 주제
    last_used = ctx.repo.persona_topic_last_used()
    weekly = ctx.repo.persona_topic_counts_since(now - timedelta(days=7))
    epoch = datetime.min.replace(tzinfo=now.tzinfo)
    limit = s.topic_weekly_limit

    def order(t: str) -> tuple:
        over = bool(limit) and weekly.get(t, 0) >= limit
        return (over, last_used.get(t, epoch), -len(groups[t]))

    for topic in sorted(groups, key=order):
        if limit and weekly.get(topic, 0) >= limit:
            log.info(
                "주제 '%s'는 최근 7일 원고 %d건으로 주간 상한에 닿아 뒤로 미룸",
                topic,
                weekly[topic],
            )
        chosen: list[tuple[SourceItem, float]] = []
        model_dups: list[str] = []
        for item, rel in sorted(groups[topic], key=lambda x: -x[1]):
            # 2차: 제목이 달라도 같은 소식이면(분류기) 뺀다. 묶음이 찰 때까지만 묻는다
            if _is_duplicate_by_model(ctx, item, titles):
                model_dups.append(item.id)
                continue
            chosen.append((item, rel))
            if len(chosen) >= per_content:
                break
        if model_dups:
            ctx.repo.mark_items_used(model_dups, DUPLICATE_EVENT)
            log.info("최근 게시물과 같은 소식(분류기)으로 자료 %d건 제외", len(model_dups))
        if chosen:
            return topic, chosen
    return None
