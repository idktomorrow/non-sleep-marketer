"""publish 잡: 승인된 콘텐츠를 플랫폼별로 하나씩 게시한다.

- 플랫폼마다 한 번에 1건, 하루 쿼터·게시 간 최소 간격을 지킨다.
- 상태를 `publishing`으로 먼저 바꾼 뒤(조건부 UPDATE, 이중 게시 방지) 어댑터를 부른다.
- 실패하면 15분·1시간·4시간 뒤 재시도, 그 뒤에도 실패하면 abandoned + 콘텐츠를 검토 대기열로.
- 재시작 시 `publishing`에 남은 건은 자동 재시도하지 않는다(`recover_interrupted`, ADR 0008).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nsm.domain.models import Content, Platform, Publication, RenderedPost
from nsm.domain.policy import (
    apply_labels,
    fit_with_tail,
    labeled_blog_title,
    local_day_bounds,
    should_post,
)
from nsm.domain.quality import card_sources, sources_line
from nsm.engine.context import JobContext, JobResult
from nsm.engine.rate_limit import blocked_until, resting, retry_at
from nsm.store.repo import InvalidTransitionError

log = logging.getLogger(__name__)

PLATFORM_NAMES = {"instagram": "인스타그램", "threads": "스레드", "naver_blog": "네이버 블로그"}

PAUSED_KEY = "paused_platforms"  # health 잡이 로그인 만료 플랫폼을 적는다


def labeled_deck(content: Content):
    """렌더용 덱: 표기를 채우고, 마무리 장 출처는 긴 주소 대신 사이트 이름으로 보인다."""
    deck = content.generated.deck
    labels = deck.labels.model_copy(update={"virtual_person": True, "ad": content.is_ad})
    slides = [
        s.model_copy(update={"sources": card_sources(list(s.sources))}) if s.kind == "outro" else s
        for s in deck.slides
    ]
    return deck.model_copy(update={"labels": labels, "slides": slides})


class CardOverflowError(Exception):
    """최소 글자 크기에서도 글이 카드에 다 들어가지 않음. 잘린 카드를 올리지 않고 사람에게 넘긴다."""


def render_images(ctx: JobContext, content: Content) -> list[Path]:
    """콘텐츠당 한 번만 렌더하고 플랫폼끼리 같은 PNG를 쓴다."""
    out = ctx.media_dir / "cards" / content.id
    deck = labeled_deck(content)
    # 덱 내용(문구·표기 포함)과 렌더러 버전(템플릿 파일)의 지문이 같을 때만 재사용한다.
    # 문구나 템플릿(예: 표기 배지 위치)을 고친 뒤 옛 이미지가 나가면 안 된다.
    version = str(getattr(ctx.renderer, "version", ""))
    fingerprint = hashlib.sha256(
        (deck.model_dump_json() + "|" + version).encode("utf-8")
    ).hexdigest()
    stamp = out / "deck.sha256"
    overflow_mark = out / "overflow.txt"
    existing = sorted(out.glob("slide-*.png")) if out.exists() else []
    if (
        stamp.exists()
        and stamp.read_text(encoding="utf-8") == fingerprint
        and len(existing) == len(deck.slides)
    ):
        if overflow_mark.exists() and overflow_mark.read_text(encoding="utf-8").strip():
            raise CardOverflowError(
                f"카드 {overflow_mark.read_text(encoding='utf-8').strip()}장 글이 넘침"
            )
        return existing
    if ctx.renderer is None:
        raise RuntimeError("렌더러가 설정되지 않았습니다")
    for old in existing:
        old.unlink()
    paths = ctx.renderer.render(deck, out)
    overflow = list(getattr(ctx.renderer, "last_overflow", []) or [])
    overflow_mark.write_text(",".join(map(str, overflow)), encoding="utf-8")
    stamp.write_text(fingerprint, encoding="utf-8")
    if overflow:
        raise CardOverflowError(f"카드 {','.join(map(str, overflow))}장 글이 넘침")
    return paths


_TEXT_FIELD = {
    "instagram": "instagram_caption",
    "threads": "threads_text",
    "naver_blog": "blog_markdown",
}


def _sources_tail(content: Content) -> str:
    """SNS 글 끝에 붙일 출처 줄(배치 Q1). 카드 마무리 장의 출처에서 사이트 이름 1~2개."""
    outro = next((s for s in content.generated.deck.slides if s.kind == "outro"), None)
    line = sources_line(list(outro.sources) if outro else [])
    return f"\n\n{line}" if line else ""


def build_post(content: Content, platform: Platform, images: list[Path]) -> RenderedPost:
    g = content.generated
    body = getattr(g, _TEXT_FIELD[platform])
    return RenderedPost(
        content_id=content.id,
        platform=platform,
        title=labeled_blog_title(g.blog_title) if platform == "naver_blog" else None,
        text=fit_with_tail(
            apply_labels(body, content.is_ad),
            platform,
            _sources_tail(content) if platform in ("instagram", "threads") else "",
        ),
        images=images,
    )


def recover_interrupted(ctx: JobContext) -> int:
    """프로세스가 게시 도중 죽었을 때: 실제로 올라갔는지 모르므로 사람에게 확인을 맡긴다."""
    n = 0
    for pub in ctx.repo.publications_in_status("publishing"):
        try:
            ctx.repo.transition_publication(
                pub.id, "abandoned", error="게시 중 중단됨, 플랫폼에서 실제 게시 여부 확인 필요"
            )
            _back_to_review(
                ctx, pub.content_id, f"{pub.platform} 게시 중 프로그램이 중단됨. 플랫폼 확인 필요"
            )
            n += 1
        except InvalidTransitionError:
            continue
    if n:
        ctx.notifier.notify(
            "게시 중단 복구", f"게시 도중 중단된 {n}건을 검토 대기열로 옮겼습니다", "warn"
        )
    return n


def _back_to_review(ctx: JobContext, content_id: str, reason: str) -> None:
    content = ctx.repo.get_content(content_id)
    if content and content.status == "approved":
        ctx.repo.transition_content(content_id, "pending_review", reason=reason)


def _record_failure(
    ctx: JobContext, pub: Publication, content: Content, attempts: int, msg: str
) -> None:
    delays = ctx.config.settings.retry_delays_minutes
    if attempts > len(delays):
        ctx.repo.transition_publication(pub.id, "abandoned", error=msg)
        _back_to_review(ctx, content.id, f"{pub.platform} 게시 {attempts}회 실패: {msg}")
        ctx.notifier.notify(
            "게시 포기", f"{pub.platform} 게시가 {attempts}회 실패했습니다: {msg}", "error"
        )
    else:
        retry_at = ctx.clock.now() + timedelta(minutes=delays[attempts - 1])
        ctx.repo.transition_publication(pub.id, "failed", error=msg, next_attempt_at=retry_at)


def run_publish(ctx: JobContext, manual: bool = False) -> JobResult:
    """manual(사람이 "지금 실행"): 같은 플랫폼 게시 간격은 무시한다. 하루 한도·게시 시간은 지킨다(계정 보호)."""
    result = JobResult("publish")
    now = ctx.clock.now()
    s = ctx.config.settings
    ok, why = should_post(now, ctx.config.persona, ctx.tz, s.posting_hours)
    if not ok:
        result.skipped = why
        return result
    if not ctx.adapters:
        result.errors.append(
            "연결된 플랫폼 어댑터가 없습니다(dry_run: false인데 실계정 어댑터 미구현, 배치 G)"
        )
        result.ok = False
        return result
    start, end = local_day_bounds(now, ctx.tz)
    paused = set(ctx.repo.get_setting(PAUSED_KEY, []))
    published = failed = 0
    waits: list[str] = []  # 이번에 안 올린 이유(플랫폼별). 사람이 "왜 0건?"을 바로 알게.
    for platform in s.enabled_platforms:
        name = PLATFORM_NAMES.get(platform, platform)
        adapter = ctx.adapters.get(platform)
        if adapter is None or platform in paused:
            waits.append(f"{name} 일시 중지됨" if platform in paused else f"{name} 연결 안 됨")
            continue
        if until := blocked_until(adapter, now):  # 사람이 누른 게시하기도 계정 보호를 위해 쉰다
            waits.append(resting(ctx, platform, until))
            continue
        quota = s.quotas.get(platform, 0)
        if ctx.repo.count_published_between(platform, start, end) >= quota:
            waits.append(f"{name} 오늘 {quota}건 다 올림(내일 다시)")
            continue
        last = ctx.repo.last_published_at(platform)
        gap = timedelta(hours=s.min_hours_between_posts)
        if not manual and last and now - last < gap:
            at = (last + gap).astimezone(ZoneInfo(ctx.tz)).strftime("%H:%M")
            waits.append(f"{name} {s.min_hours_between_posts:g}시간 간격 대기({at} 이후)")
            continue
        due = ctx.repo.due_publications(platform, now)
        if not due:
            waits.append(f"{name} 올릴 차례인 원고 없음(재시도 시각 전이거나 대기 없음)")
            continue
        outcome = _publish_one(ctx, adapter, due[0], result)
        published += outcome == "published"
        failed += outcome == "failed"
    result.counts.update(published=published, failed=failed)
    if waits:
        result.counts["waits"] = waits
    result.ok = not result.errors
    return result


def _publish_one(ctx: JobContext, adapter, pub: Publication, result: JobResult) -> str:
    content = ctx.repo.get_content(pub.content_id)
    if content is None:
        return "skipped"
    try:
        ctx.repo.transition_publication(pub.id, "publishing", increment_attempts=True)
    except InvalidTransitionError:
        return "skipped"  # 다른 실행이 먼저 잡았다
    attempts = pub.attempts + 1
    try:
        images = render_images(ctx, content)
    except CardOverflowError as e:
        # 재시도해도 같은 결과다: 바로 사람에게(문구를 줄여 고친 뒤 다시 승인)
        msg = f"{e}. 검토 화면에서 문구를 줄여 주세요"
        result.errors.append(f"{pub.platform}: {msg}")
        ctx.repo.transition_publication(pub.id, "abandoned", error=msg)
        _back_to_review(ctx, content.id, msg)
        ctx.notifier.notify("카드 글 넘침", msg, "warn")
        return "failed"
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:500]
        result.errors.append(f"{pub.platform} 렌더 실패: {msg}")
        try:
            _record_failure(ctx, pub, content, attempts, msg)
        except Exception as e2:  # noqa: BLE001
            log.exception("게시 실패 기록 중 오류")
            result.errors.append(f"{pub.platform} 실패 기록 오류: {e2}")
        return "failed"
    try:
        res = adapter.publish(build_post(content, pub.platform, images))
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:500]
        result.errors.append(f"{pub.platform} 게시 실패: {msg}")
        if until := retry_at(e):
            # 원고 탓이 아니다: 재시도 횟수를 쓰지 않고(포기·검토로 돌리지 않음) 쉬는 끝 시각에 다시
            resting(ctx, pub.platform, until)
            ctx.repo.transition_publication(
                pub.id, "failed", error=msg, next_attempt_at=until, refund_attempt=True
            )
            return "failed"
        try:
            _record_failure(ctx, pub, content, attempts, msg)
        except Exception as e2:  # noqa: BLE001  실패 기록 실패가 다른 플랫폼 게시를 막지 않게
            log.exception("게시 실패 기록 중 오류")
            result.errors.append(f"{pub.platform} 실패 기록 오류: {e2}")
        return "failed"
    ctx.repo.transition_publication(pub.id, "published", external_id=res.external_id, url=res.url)
    ctx.repo.mark_content_published_if_complete(content.id)
    log.info("%s 게시 완료: %s", pub.platform, res.url)
    return "published"
