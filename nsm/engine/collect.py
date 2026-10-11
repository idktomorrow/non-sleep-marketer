"""collect 잡: 소스에서 자료를 모으고(중복 제거) 관련성·주제를 분류해 저장한다."""

from __future__ import annotations

import logging

from nsm.domain.policy import should_act
from nsm.engine.context import JobContext, JobResult
from nsm.ports import AdapterError

log = logging.getLogger(__name__)

RELEVANCE_LABELS = ["relevant", "irrelevant"]


def relevance_instruction(ctx: JobContext) -> str:
    b, p = ctx.config.brand, ctx.config.persona
    return (
        f"이 자료가 브랜드 '{b.name}'({b.industry})의 SNS 콘텐츠 소재로 쓸 만큼 "
        f"다음 관심 주제와 관련 있는가? 관심 주제: {', '.join(p.topics)}. "
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
    new = dup = 0
    for source in ctx.sources:
        try:
            items = source.collect(topics, s.collect.per_source_limit)
        except Exception as e:  # noqa: BLE001  소스 하나가 실패해도 나머지는 계속
            log.warning("소스 %s 수집 실패: %s", getattr(source, "name", source), e)
            result.errors.append(f"{getattr(source, 'name', source)}: {e}")
            continue
        for item in items:
            if ctx.repo.insert_source_item(item):
                new += 1
            else:
                dup += 1
    result.counts.update(new=new, duplicate=dup)

    relevant, classified = _classify_pending(ctx, result)
    result.counts.update(classified=classified, relevant=relevant)
    result.ok = not result.errors or new > 0 or classified > 0
    return result


def _classify_pending(ctx: JobContext, result: JobResult) -> tuple[int, int]:
    if ctx.relevance_classifier is None or ctx.topic_classifier is None:
        result.errors.append("분류기가 설정되지 않았습니다")
        return 0, 0
    pending = ctx.repo.unclassified_items()
    if not pending:
        return 0, 0
    threshold = ctx.config.settings.relevance_threshold
    topics = ctx.config.persona.topics
    texts = [f"{i.title}\n{i.text}" for i in pending]
    try:
        rel = ctx.relevance_classifier.classify_many(
            texts, RELEVANCE_LABELS, relevance_instruction(ctx)
        )
    except (AdapterError, Exception) as e:  # noqa: BLE001
        result.errors.append(f"관련성 분류 실패: {e}")
        return 0, 0

    relevant = 0
    for item, text, c in zip(pending, texts, rel, strict=True):
        # "relevant일 확률"로 통일해서 저장한다
        score = c.confidence if c.label == "relevant" else 1.0 - c.confidence
        ctx.repo.set_source_relevance(item.id, score)
        ctx.repo.record_classification(
            "source_item", item.id, "relevance", c.label, c.confidence, ctx.classifier_name
        )
        if score < threshold:
            continue
        relevant += 1
        try:
            t = ctx.topic_classifier.classify(text, topics, topic_instruction(ctx))
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"주제 분류 실패({item.id}): {e}")
            continue
        ctx.repo.set_source_topic(item.id, t.label, t.confidence)
        ctx.repo.record_classification(
            "source_item", item.id, "topic", t.label, t.confidence, ctx.classifier_name
        )
    return relevant, len(pending)
