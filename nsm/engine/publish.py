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
    has_ai_image,
    labeled_blog_title,
    local_day_bounds,
    should_post,
)
from nsm.domain.quality import card_sources
from nsm.engine.artifacts import ArtifactMissing
from nsm.engine.context import JobContext, JobResult
from nsm.engine.rate_limit import blocked_until, resting, retry_at
from nsm.store.repo import InvalidTransitionError

log = logging.getLogger(__name__)

PLATFORM_NAMES = {"instagram": "인스타그램", "threads": "스레드", "naver_blog": "네이버 블로그"}

PAUSED_KEY = "paused_platforms"  # health 잡이 로그인 만료 플랫폼을 적는다


def ai_slides_from_assets(deck) -> list[int]:
    """장별 AI 표기(S14·S15): 그 장의 그림 자산(스티커·만화 컷)의 출처로 정한다. 장 순서가 바뀌어도 자산 기준."""
    out = []
    for i, s in enumerate(deck.slides):
        assets = [getattr(s, "sticker", None)]
        if s.kind == "comic":
            assets += [p.image for p in s.panels]
        if s.kind == "meme_image":
            assets.append(s.image)
        if any(a is not None and a.ai for a in assets):
            out.append(i)
    return out


def labeled_deck(content: Content):
    """렌더용 덱: 표기를 채우고, 마무리 장 출처는 긴 주소 대신 사이트 이름으로 보인다.
    ai_slides가 있는(새) 덱은 장별 AI 표기를 자산에서 다시 계산한다(None이면 레거시 규칙 그대로)."""
    deck = content.generated.deck
    update = {"virtual_person": True, "ad": content.is_ad}
    if deck.labels.ai_slides is not None:
        # 자산(스티커·컷)은 매번 다시 계산. 배경 장 번호는 레거시 덱을 옮길 때 기록해 둔 것만 유지한다
        # (새 콘텐츠의 []에는 레거시 플래그를 합치지 않는다)
        kept_bg = {
            i for i in deck.labels.ai_slides if i < len(deck.slides) and deck.slides[i].background
        }
        update["ai_slides"] = sorted(set(ai_slides_from_assets(deck)) | kept_bg)
    labels = deck.labels.model_copy(update=update)
    slides = [
        s.model_copy(update={"sources": card_sources(list(s.sources))}) if s.kind == "outro" else s
        for s in deck.slides
    ]
    return deck.model_copy(
        update={"labels": labels, "slides": slides, "sources": card_sources(list(deck.sources))}
    )


class CardOverflowError(Exception):
    """최소 글자 크기에서도 글이 카드에 다 들어가지 않음. 잘린 카드를 올리지 않고 사람에게 넘긴다."""


def render_images(ctx: JobContext, content: Content) -> list[Path]:
    """게시할 카드(S15): 승인 산출물이 있으면 그것만 쓴다(다시 그리지 않음). 판이 다르거나 깨졌으면 ArtifactMissing.
    산출물이 없으면(정책 자동 승인·예전 원고) 한 번 그린 뒤 그 판의 산출물로 굳혀 플랫폼·재시도가 같은 PNG를 쓴다."""
    from nsm.engine.artifacts import artifact_pngs, files_sha, freeze
    from nsm.store.production import ProductionStore

    prod = ProductionStore(ctx.repo.db, ctx.clock)
    revision = ctx.repo.content_revision(content.id)
    frozen = artifact_pngs(prod, content, revision)
    if frozen is not None:
        return frozen
    paths = _render_cached(ctx, content)
    freeze(prod, ctx.media_dir, content, revision, paths, files_sha(paths), None, "policy")
    return artifact_pngs(prod, content, revision) or paths


def _render_cached(ctx: JobContext, content: Content) -> list[Path]:
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


def post_source_urls(content: Content) -> list[str]:
    """게시 글 끝에 적을 실제 주소(S18 사용자 결정 2026-10-23: 카드에는 출처 표기를 그리지 않고 글에 진짜 주소).
    사실 자료(덱 출처·마무리 장 출처)와 카드에 넣은 실제 사진의 출처를 한 번씩."""
    deck = content.generated.deck
    outro = next((s for s in deck.slides if s.kind == "outro"), None)
    # 마무리 장이 있으면 그 출처(검토 화면에서 고친 값)가 우선, 없으면 덱 출처
    urls = list(outro.sources) if outro is not None else list(deck.sources)
    for s in deck.slides:
        ph = getattr(s, "photo", None)
        if ph is not None and ph.source:
            urls.append(ph.source)
    return list(dict.fromkeys(u for u in urls if u and u.strip()))


def _sources_tail(content: Content, platform: str = "instagram") -> str:
    """SNS 글 끝 출처: 실제 주소. 스레드는 글자 수 상한(500자)이 있어 1개만."""
    all_src = post_source_urls(content)
    urls = [u for u in all_src if u.startswith(("http://", "https://"))]
    names = [
        u for u in all_src if u not in urls
    ]  # "브랜드 제공 자료"처럼 주소가 아닌 출처는 이름 그대로
    shown = (urls[:1] if platform == "threads" else urls[:4]) + names[:2]
    return "\n\n출처\n" + "\n".join(shown) if shown else ""


def build_post(
    content: Content, platform: Platform, images: list[Path], frozen: dict | None = None
) -> RenderedPost:
    g = content.generated
    key = {"instagram": "instagram", "threads": "threads", "naver_blog": "blog_markdown"}[platform]
    # 승인 산출물에 굳힌 캡션이 있으면 그것(검토한 판 = 게시하는 판, S15)
    body = frozen[key] if frozen and key in frozen else getattr(g, _TEXT_FIELD[platform])
    return RenderedPost(
        content_id=content.id,
        platform=platform,
        title=labeled_blog_title(g.blog_title) if platform == "naver_blog" else None,
        text=fit_with_tail(
            apply_labels(body, content.is_ad, has_ai_image(g.deck)),
            platform,
            _sources_tail(content, platform) if platform in ("instagram", "threads") else "",
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
    except ArtifactMissing as e:
        # 승인한 판과 다르거나 파일이 사라졌다: 다시 그려 올리지 않고 사람에게(S15)
        msg = f"{e}. 검토 화면에서 미리보기를 확인하고 다시 승인해 주세요"
        result.errors.append(f"{pub.platform}: {msg}")
        ctx.repo.transition_publication(pub.id, "abandoned", error=msg)
        _back_to_review(ctx, content.id, msg)
        ctx.notifier.notify("승인 산출물 없음", msg, "warn")
        return "failed"
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
        from nsm.engine.artifacts import captions
        from nsm.store.production import ProductionStore

        frozen = captions(ProductionStore(ctx.repo.db, ctx.clock), content.id)
        res = adapter.publish(build_post(content, pub.platform, images, frozen))
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
