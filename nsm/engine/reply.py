"""reply 잡: 내 게시물의 새 댓글을 분류하고 자동 답글·숨김·사람에게 넘기기를 한다.

- 같은 댓글은 한 번만 처리한다(DB 키: 플랫폼 + 댓글 ID).
- 분류가 불확실하면(신뢰도 < 0.6) 사람에게 넘긴다.
- 답글에도 금칙어 검사를 한다. 걸리면 보내지 않고 사람에게 넘긴다.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from nsm.domain.models import COMMENT_CATEGORIES, Comment
from nsm.domain.policy import decide_comment, find_banned, local, should_act
from nsm.engine.context import JobContext, JobResult
from nsm.engine.publish import PAUSED_KEY

log = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.6
INSTRUCTION = (
    "SNS 게시물에 달린 댓글을 분류하라. praise_general=칭찬·일반 반응, question=질문, "
    "complaint=불만, price_refund=가격·환불·결제 문의, legal=법적 문제·권리 침해 주장, "
    "spam=광고·홍보·사기성 링크."
)


def _post_context(ctx: JobContext, c: Comment) -> str:
    content = ctx.repo.content_for_post(c.platform, c.post_external_id)
    if content is None:
        return ""
    g = content.generated
    return f"{g.topic}\n{g.threads_text}"


def run_reply(ctx: JobContext) -> JobResult:
    result = JobResult("reply")
    now = ctx.clock.now()
    ok, why = should_act(now, ctx.config.persona, ctx.tz)
    if not ok:
        result.skipped = why
        return result
    if ctx.comment_classifier is None:
        result.skipped = "댓글 분류기가 없음"
        return result
    s = ctx.config.settings
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []))
    counts = dict.fromkeys(("new", "auto_replied", "hidden", "escalated", "ignored"), 0)
    for platform, adapter in ctx.adapters.items():
        if platform in paused or "fetch_comments" not in adapter.capabilities():
            continue
        posts = ctx.repo.published_posts_since(
            platform, now - timedelta(days=s.comment_lookback_days)
        )
        if not posts:
            continue
        try:
            comments = adapter.fetch_new_comments(
                [p.external_id for p in posts if p.external_id],
                ctx.repo.latest_comment_time(platform),
            )
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"{platform} 댓글 조회 실패: {e}")
            continue
        for c in sorted(comments, key=lambda x: x.created_at):
            if not ctx.repo.insert_comment(c):
                continue
            counts["new"] += 1
            try:
                outcome = _handle(ctx, adapter, c)
            except Exception as e:  # noqa: BLE001  한 댓글 실패가 나머지를 막지 않게
                log.warning("댓글 처리 실패 %s: %s", c.external_id, e)
                result.errors.append(f"댓글 {c.external_id}: {e}")
                ctx.repo.update_comment(
                    ctx.repo.comment_key(c), status="escalated", reason=f"자동 처리 실패: {e}"[:300]
                )
                outcome = "escalated"
            counts[outcome] += 1
    result.counts.update(counts)
    if counts["escalated"]:
        ctx.notifier.notify(
            "댓글 확인 필요", f"사람이 확인해야 할 댓글이 {counts['escalated']}건 있습니다", "warn"
        )
    result.ok = not result.errors or counts["new"] > 0
    return result


def _handle(ctx: JobContext, adapter, c: Comment) -> str:
    key = ctx.repo.comment_key(c)
    s = ctx.config.settings
    cls = ctx.comment_classifier.classify(c.text, list(COMMENT_CATEGORIES), INSTRUCTION)  # type: ignore[union-attr]
    category = cls.label
    if category not in COMMENT_CATEGORIES:
        category = "question"
    today = local(ctx.clock.now(), ctx.tz).date()
    replies_today = ctx.repo.reply_count(c.platform, c.author_id, today)
    action, reason = decide_comment(
        category,  # type: ignore[arg-type]
        replies_today,
        s.reply_daily_limit_per_author,
        adapter.capabilities(),
    )
    if action in ("auto_reply", "hide") and cls.confidence < MIN_CONFIDENCE:
        action, reason = "escalate", f"분류 불확실({category}, 신뢰도 {cls.confidence:.2f})"

    def record(status: str, **kw) -> str:
        ctx.repo.update_comment(
            key,
            status=status,
            category=category,  # type: ignore[arg-type]
            confidence=cls.confidence,
            action=action if status != "escalated" else "escalate",
            reason=kw.pop("reason", reason) or None,
            handled_by="policy" if status != "escalated" else None,
            **kw,
        )
        return status

    if action == "ignore":
        return record("ignored")
    if action == "hide":
        adapter.hide_comment(c)
        return record("hidden")
    if action == "escalate":
        return record("escalated")

    # auto_reply
    if ctx.text_generator is None:
        return record("escalated", reason="자동 답글 생성기가 없음(Claude API 키 필요)")
    text = ctx.text_generator.generate_reply(c, ctx.config.persona, _post_context(ctx, c))
    banned = find_banned([text], ctx.config.persona)
    if banned or not text.strip():
        return record(
            "escalated",
            reason=f"답글 초안에 금칙어: {', '.join(banned)}"
            if banned
            else "답글 초안이 비어 있음",
            reply_text=text or None,
        )
    reply_id = adapter.reply(c, text)
    ctx.repo.increment_reply_count(c.platform, c.author_id, local(ctx.clock.now(), ctx.tz).date())
    return record("auto_replied", reply_text=text, reply_external_id=reply_id)
