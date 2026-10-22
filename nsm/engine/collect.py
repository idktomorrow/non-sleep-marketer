"""collect 잡: 소스에서 자료를 모으고(중복 제거) 관련성·주제를 분류해 저장한다."""

from __future__ import annotations

import logging

from nsm.domain.models import Classification, SourceItem
from nsm.domain.policy import should_act
from nsm.domain.quality import excluded_genre
from nsm.engine.context import JobContext, JobResult

log = logging.getLogger(__name__)

RELEVANCE_LABELS = ["relevant", "irrelevant"]
FACTS_LABELS = ["has_facts", "no_facts"]
FACTS_INSTRUCTION = (
    "이 글에 SNS 정보 카드에 쓸 수 있는 구체적 사실(날짜, 가격, 수량, 장소·판매처 이름, 상품·행사 이름, "
    "순위·관객 수 같은 숫자)이 있으면 has_facts, 일반적인 설명·감상·'검색해 보세요' 수준의 안내뿐이면 no_facts로 답하라."
)


def excluded_word(item: SourceItem, words: list[str]) -> str | None:
    """수집 제외어(설정) 중 제목·본문에 든 첫 낱말. 대소문자는 가리지 않는다(C15)."""
    hay = f"{item.title}\n{item.text}".lower()
    return next((w for w in words if w.strip() and w.lower() in hay), None)


def relevance_instruction(ctx: JobContext, extra_topic: str | None = None) -> str:
    b, p = ctx.config.brand, ctx.config.persona
    topics = [*p.topics, extra_topic] if extra_topic else p.topics
    # 모든 분류기가 같은 지시를 쓴다(ADR 0003·0048). 2026-10-12 드라이런에서 카페 방문 후기가
    # 관련 0.93으로 통과해, "정보성 글만"과 제외 장르를 명시한다.
    return (
        f"이 자료가 브랜드 '{b.name}'({b.industry})의 SNS 정보 콘텐츠 소재로 쓸 수 있는가? "
        f"관심 주제: {', '.join(topics)}. "
        "관심 주제와 직접 관련된 정보성 글(방법, 기준, 원리, 비교, 정리, 소식)만 relevant다. "
        "주제 낱말이 나와도 방문 후기, 맛집·카페 리뷰, 여행기, 개인 일기·일상 기록, 구매·사용 후기, "
        "광고 홍보 글은 irrelevant다. "
        # C27: Gemini가 흥행·품절 기사를 "정보성 글이 아님"으로 보던 것을 이 한 줄이 바로잡았다(ADR 0048)
        "관심 주제의 흥행·판매·품절·행사 같은 뉴스 기사와 공식 소식 정리는 relevant다(언론 보도 포함). "
        "relevant 또는 irrelevant로 답하라."
    )


def topic_instruction(ctx: JobContext) -> str:
    return "이 자료가 다루는 주제로 가장 가까운 것을 하나 고르라."


def run_collect(ctx: JobContext) -> JobResult:
    result = JobResult("collect")
    ok, why = should_act(ctx.clock.now(), ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result

    s = ctx.config.settings
    topics = ctx.config.persona.topics
    words = s.collect.exclude_words
    new = dup = excluded = 0
    for source in ctx.sources:
        try:
            items = source.collect(topics, s.collect.per_source_limit)
        except Exception as e:  # noqa: BLE001  소스 하나가 실패해도 나머지는 계속
            log.warning("소스 %s 수집 실패: %s", getattr(source, "name", source), e)
            result.errors.append(f"{getattr(source, 'name', source)}: {e}")
            continue
        for item in items:
            if excluded_word(item, words):
                excluded += 1  # 제외어가 든 글은 저장하지 않는다(분류 비용도 들지 않음)
                continue
            if ctx.repo.insert_source_item(item):
                new += 1
            else:
                dup += 1
    result.counts.update(new=new, duplicate=dup)
    if excluded:
        result.counts["excluded_word"] = excluded

    relevant, classified = _classify_pending(ctx, result)
    result.counts.update(classified=classified, relevant=relevant)
    result.counts["bodies"] = _fetch_bodies(ctx, result)
    result.counts["facts_judged"] = _judge_facts(ctx, result)
    result.ok = not result.errors or new > 0 or classified > 0
    return result


def _who(ctx: JobContext, clf: object) -> str:
    """분류 기록에 적을 분류기 이름. 대체 분류기로 넘어갔으면 넘어간 쪽(C27)."""
    return str(getattr(clf, "name", None) or ctx.classifier_name)


def _classify_pending(ctx: JobContext, result: JobResult) -> tuple[int, int]:
    if ctx.relevance_classifier is None or ctx.topic_classifier is None:
        result.errors.append("분류기가 설정되지 않았습니다")
        return 0, 0
    threshold = ctx.config.settings.relevance_threshold
    pending = []
    for item in ctx.repo.unclassified_items():
        hit = excluded_genre(item.title, item.text)
        if hit:
            # 후기·여행기·일기는 분류기를 부르지 않고 제외한다(비용도 아낀다)
            ctx.repo.set_source_relevance(item.id, 0.0)
            ctx.repo.record_classification(
                "source_item", item.id, "relevance", "irrelevant", 1.0, f"rule:{hit}"
            )
            result.counts["excluded_genre"] = result.counts.get("excluded_genre", 0) + 1
            continue
        pending.append(item)
    texts = [f"{i.title}\n{i.text}" for i in pending]
    instruction = relevance_instruction(ctx)
    try:
        rel: list[Classification | None] = list(
            ctx.relevance_classifier.classify_many(texts, RELEVANCE_LABELS, instruction)
        )
    except Exception as e:  # noqa: BLE001  일괄 실패 → 한 건씩 다시 시도
        log.warning("관련성 일괄 분류 실패, 한 건씩 재시도: %s", e)
        rel = []
        for text in texts:
            try:
                rel.append(ctx.relevance_classifier.classify(text, RELEVANCE_LABELS, instruction))
            except Exception as e2:  # noqa: BLE001
                result.errors.append(f"관련성 분류 실패: {e2}")
                rel.append(None)

    relevant = 0
    for item, c in zip(pending, rel, strict=True):
        if c is None:
            # 같은 항목이 매 실행마다 실패하며 비용만 쓰지 않도록 관련성 0으로 닫는다
            ctx.repo.set_source_relevance(item.id, 0.0)
            ctx.repo.record_classification(
                "source_item",
                item.id,
                "relevance",
                "error",
                0.0,
                _who(ctx, ctx.relevance_classifier),
            )
            continue
        # "relevant일 확률"로 통일해서 저장한다
        score = c.confidence if c.label == "relevant" else 1.0 - c.confidence
        ctx.repo.set_source_relevance(item.id, score)
        ctx.repo.record_classification(
            "source_item",
            item.id,
            "relevance",
            c.label,
            c.confidence,
            _who(ctx, ctx.relevance_classifier),
        )
        if score >= threshold:
            relevant += 1

    # 관련 자료 중 주제가 아직 없는 것(이번 것 + 지난번 주제 분류 실패분)
    topics = ctx.config.persona.topics
    todo = ctx.repo.relevant_items_without_topic(threshold)
    todo_texts = [f"{i.title}\n{i.text}" for i in todo]
    # 관련성처럼 묶어서 보낸다(Gemini 무료 구간은 분당 요청 수가 작다, C27). 묶음이 실패하면 한 건씩
    try:
        ts: list[Classification | None] = list(
            ctx.topic_classifier.classify_many(todo_texts, topics, topic_instruction(ctx))
        )
    except Exception as e:  # noqa: BLE001
        log.warning("주제 일괄 분류 실패, 한 건씩 재시도: %s", e)
        ts = []
        for item, text in zip(todo, todo_texts, strict=True):
            try:
                ts.append(ctx.topic_classifier.classify(text, topics, topic_instruction(ctx)))
            except Exception as e2:  # noqa: BLE001  다음 실행에서 다시 시도된다
                result.errors.append(f"주제 분류 실패({item.id}): {e2}")
                ts.append(None)
    for item, t in zip(todo, ts, strict=True):
        if t is None:
            continue
        ctx.repo.set_source_topic(item.id, t.label, t.confidence)
        ctx.repo.record_classification(
            "source_item", item.id, "topic", t.label, t.confidence, _who(ctx, ctx.topic_classifier)
        )
    return relevant, len(pending) + result.counts.get("excluded_genre", 0)


def _fetch_bodies(ctx: JobContext, result: JobResult, items: list[SourceItem] | None = None) -> int:
    """관련 자료로 판정된 웹 글만 원문 본문을 가져온다(ADR 0022). 실패하면 검색 요약문을 그대로 쓴다.

    items: 이 자료들만(키워드 원고가 고른 자료, C15). 이미 원문을 읽은 것은 건너뛴다.
    """
    by_name = {getattr(s, "name", ""): s for s in ctx.sources}
    fetched = 0
    threshold = ctx.config.settings.relevance_threshold
    if items is None:
        todo = ctx.repo.relevant_items_without_body(
            threshold, ctx.config.settings.max_bodies_per_run
        )
    else:
        todo = [i for i in items if not ctx.repo.body_done(i.id)]
    for item in todo:
        if not item.url or item.url.startswith("file:"):
            # 업로드 자료는 원문이 이미 본문이다
            ctx.repo.record_classification(
                "source_item", item.id, "body", "skipped", 0.0, "collect"
            )
            continue
        source = by_name.get(item.source)
        if source is None:
            continue  # 이번 실행에 그 소스(브라우저)가 없음: 기록하지 않고 다음 실행에 다시
        fetch = getattr(source, "fetch_body", None)
        if not callable(fetch):
            # 이 소스는 원문 읽기가 아예 없다(요약문이 전부): 결론을 내 두어야 구체 사실 판정이 진행된다(C31)
            ctx.repo.record_classification(
                "source_item", item.id, "body", "skipped", 0.0, "collect"
            )
            continue
        try:
            body = fetch(item.url)
        except Exception as e:  # noqa: BLE001  본문 실패는 요약문으로 대신한다
            log.info("본문 가져오기 실패(%s): %s", item.url, e)
            body = None
        if body:
            ctx.repo.set_source_text(item.id, body)
            ctx.repo.set_source_facts(
                item.id, None
            )  # 요약문으로 판정한 게 있으면 본문으로 다시(C31)
            fetched += 1
        ctx.repo.record_classification(
            "source_item",
            item.id,
            "body",
            "fetched" if body else "failed",
            1.0 if body else 0.0,
            "collect",
        )
    return fetched


def _judge_facts(ctx: JobContext, result: JobResult) -> int:
    """원문 읽기가 끝난 관련 자료에 구체 사실이 있는지 판정해 둔다(C31). 없는 자료는 원고 재료로 안 쓴다.

    - 낱말 대체 분류기(키가 없거나 대체 중)는 이 판정을 못 하므로 비워 둔다(비어 있으면 재료 선택에서 통과).
    - 일괄 판정이 실패하면 한 건씩 다시 묻는다(수집 분류와 같은 방식). 그래도 실패한 건 다음 실행에.
    """
    if ctx.relevance_classifier is None or _who(ctx, ctx.relevance_classifier) == "keyword":
        return 0
    todo = ctx.repo.relevant_items_without_facts(ctx.config.settings.relevance_threshold)
    if not todo:
        return 0
    texts = [f"{i.title}\n{i.text[:1500]}" for i in todo]
    try:
        labels: list[Classification | None] = list(
            ctx.relevance_classifier.classify_many(texts, FACTS_LABELS, FACTS_INSTRUCTION)
        )
    except Exception as e:  # noqa: BLE001  일괄 실패 → 한 건씩
        log.warning("구체 사실 일괄 판정 실패, 한 건씩 재시도: %s", e)
        labels = []
        for text in texts:
            try:
                labels.append(
                    ctx.relevance_classifier.classify(text, FACTS_LABELS, FACTS_INSTRUCTION)
                )
            except Exception as e2:  # noqa: BLE001  다음 실행에
                log.info("구체 사실 판정 실패(다음에 다시): %s", e2)
                labels.append(None)
    n = 0
    for item, c in zip(todo, labels, strict=True):
        if c is None:
            continue
        label = "none" if c.label == "no_facts" else "has"
        ctx.repo.set_source_facts(item.id, label)
        ctx.repo.record_classification(
            "source_item",
            item.id,
            "facts",
            c.label,
            c.confidence,
            _who(ctx, ctx.relevance_classifier),
        )
        n += 1
        if label == "none":
            result.counts["no_facts"] = result.counts.get("no_facts", 0) + 1
    return n
