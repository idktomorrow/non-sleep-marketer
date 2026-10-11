"""weekly_report 잡: 지난 7일 결산을 마크다운으로 만든다 (PRD 스토리 50~55).

LLM 없이 정해진 규칙으로 계산한다(무료, 같은 데이터 → 같은 리포트).
- 가장 잘된 콘텐츠: 참여율 = (좋아요 + 댓글 + 저장 + 공유) / 조회. 이유는 평균 대비 배수와 형식·시간대·주제.
- 다음 주 추천 주제: 페르소나 주제별 평균 참여율 + 아직 안 쓴 관련 자료 수 + 관련 급상승 검색어.
- 협찬(광고) 게시물 성과는 따로 모은다(미디어킷용).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import mean
from typing import Any

from nsm.domain.policy import local
from nsm.engine.context import JobContext, JobResult
from nsm.store.db import to_db

PERIOD_DAYS = 7
TEMPLATE_KO = {"info": "정보형", "list": "리스트형", "compare": "비교형"}


def _engagement(p: dict[str, Any]) -> float | None:
    views = p.get("views") or 0
    if views <= 0:
        return None
    total = sum((p.get(k) or 0) for k in ("likes", "comments", "saves", "shares"))
    return total / views


def _period_posts(ctx: JobContext, start, end) -> list[dict[str, Any]]:
    cur = ctx.repo.db.cursor()
    try:
        cur.execute(
            "WITH latest AS ("
            "  SELECT *, row_number() OVER (PARTITION BY platform, post_external_id ORDER BY captured_at DESC) rn"
            "  FROM metrics_posts) "
            "SELECT p.platform, p.url, p.published_at, c.topic, c.template, c.is_ad, c.persona_topic, "
            "  m.views, m.likes, m.comments, m.saves, m.shares "
            "FROM publications p JOIN contents c ON c.id = p.content_id "
            "LEFT JOIN latest m ON m.platform = p.platform AND m.post_external_id = p.external_id AND m.rn = 1 "
            "WHERE p.status = 'published' AND p.published_at >= ? AND p.published_at <= ?",
            [to_db(start), to_db(end)],
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        cur.close()
    from nsm.store.db import from_db

    for r in rows:
        r["published_at"] = from_db(r["published_at"])
        r["rate"] = _engagement(r)
    return rows


def _followers(ctx: JobContext, start, end) -> dict[str, tuple[int | None, int | None]]:
    rows = ctx.repo.db.execute(
        "SELECT platform, arg_min(followers, captured_at), arg_max(followers, captured_at) "
        "FROM metrics_account WHERE captured_at >= ? AND captured_at <= ? GROUP BY platform",
        [to_db(start), to_db(end)],
    )
    return {r[0]: (r[1], r[2]) for r in rows}


def _reason(p: dict[str, Any], avg: float, tz: str) -> str:
    parts = []
    if avg > 0 and p["rate"] is not None:
        parts.append(f"참여율이 평균의 {p['rate'] / avg:.1f}배")
    parts.append(TEMPLATE_KO.get(p["template"], p["template"]))
    hour = local(p["published_at"], tz).hour
    parts.append("오전 게시" if hour < 12 else "오후 게시" if hour < 18 else "저녁 게시")
    if p.get("persona_topic"):
        parts.append(f"주제 '{p['persona_topic']}'")
    if (p.get("saves") or 0) > (p.get("likes") or 0) * 0.3:
        parts.append("저장이 많음(다시 볼 정보로 인식)")
    return ", ".join(parts)


def build_report(ctx: JobContext) -> tuple[str, dict[str, Any]]:
    now = ctx.clock.now()
    start = now - timedelta(days=PERIOD_DAYS)
    tz = ctx.tz
    posts = _period_posts(ctx, start, now)
    rated = [p for p in posts if p["rate"] is not None]
    avg = mean(p["rate"] for p in rated) if rated else 0.0
    top = sorted(rated, key=lambda p: p["rate"], reverse=True)[:3]

    by_topic: dict[str, list[float]] = defaultdict(list)
    for p in rated:
        if p.get("persona_topic"):
            by_topic[p["persona_topic"]].append(p["rate"])
    unused: dict[str, int] = defaultdict(int)
    for _item, _rel, topic in ctx.repo.unused_relevant_items(
        ctx.config.settings.relevance_threshold
    ):
        if topic:
            unused[topic] += 1
    topics = ctx.config.persona.topics
    recommended = sorted(
        topics,
        key=lambda t: (mean(by_topic[t]) if by_topic[t] else avg, unused[t]),
        reverse=True,
    )
    trends = [
        r[0]
        for r in ctx.repo.db.execute(
            "SELECT title FROM source_items WHERE source = 'google_trends' AND relevance >= ? "
            "AND collected_at >= ? ORDER BY relevance DESC LIMIT 3",
            [ctx.config.settings.relevance_threshold, to_db(start)],
        )
    ]
    followers = _followers(ctx, start, now)
    comments = dict(
        ctx.repo.db.execute(
            "SELECT status, count(*) FROM comments WHERE fetched_at >= ? GROUP BY 1", [to_db(start)]
        )
    )
    costs = dict(
        ctx.repo.db.execute(
            "SELECT kind, round(sum(usd), 2) FROM costs WHERE occurred_at >= ? GROUP BY 1",
            [to_db(start)],
        )
    )
    ads = [p for p in posts if p["is_ad"]]

    d0, d1 = local(start, tz).date(), local(now, tz).date()
    lines = [f"# 주간 리포트 {d0} ~ {d1}", ""]
    lines += [
        "## 요약",
        "",
        f"- 게시 {len(posts)}건 ("
        + ", ".join(
            f"{pl} {sum(1 for p in posts if p['platform'] == pl)}"
            for pl in sorted({p["platform"] for p in posts})
        )
        + ")"
        if posts
        else "- 게시 0건",
        f"- 평균 참여율 {avg * 100:.1f}%" if rated else "- 지표가 아직 없습니다",
        "- 댓글: " + (", ".join(f"{k} {v}" for k, v in sorted(comments.items())) or "없음"),
        "- 비용: "
        + (", ".join(f"{k} ${v:.2f}" for k, v in sorted(costs.items())) or "$0.00")
        + (f" (합계 ${sum(costs.values()):.2f})" if costs else ""),
        "",
        "## 팔로워",
        "",
    ]
    if followers:
        for pl, (a, b) in sorted(followers.items()):
            delta = (b or 0) - (a or 0)
            lines.append(f"- {pl}: {a} → {b} ({'+' if delta >= 0 else ''}{delta})")
    else:
        lines.append("- 기록 없음")
    lines += ["", "## 가장 잘된 콘텐츠", ""]
    if top:
        for i, p in enumerate(top, 1):
            lines.append(
                f"{i}. **{p['topic']}** ({p['platform']}) 참여율 {p['rate'] * 100:.1f}%, 조회 {p['views']}"
            )
            lines.append(f"   - 이유: {_reason(p, avg, tz)}")
    else:
        lines.append("- 아직 비교할 지표가 없습니다")
    lines += ["", "## 다음 주 추천 주제", ""]
    for t in recommended[:3]:
        rate = (
            f"평균 참여율 {mean(by_topic[t]) * 100:.1f}%" if by_topic[t] else "아직 성과 기록 없음"
        )
        lines.append(f"- **{t}**: {rate}, 쓸 수 있는 자료 {unused[t]}건")
    if trends:
        lines.append(f"- 관련 급상승 검색어: {', '.join(trends)}")
    lines += ["", "## 협찬(광고) 게시물", ""]
    if ads:
        for p in ads:
            rate = f"{p['rate'] * 100:.1f}%" if p["rate"] is not None else "-"
            lines.append(
                f"- {p['topic']} ({p['platform']}): 조회 {p['views'] or '-'}, 참여율 {rate}"
            )
    else:
        lines.append("- 이번 주 협찬 게시물 없음")
    lines.append("")
    summary = {
        "posts": len(posts),
        "top": [p["topic"] for p in top],
        "recommended": recommended[:3],
    }
    return "\n".join(lines), summary


def run_weekly_report(ctx: JobContext) -> JobResult:
    result = JobResult("weekly_report")
    text, summary = build_report(ctx)
    ctx.reports_dir.mkdir(parents=True, exist_ok=True)
    day = local(ctx.clock.now(), ctx.tz).date().isoformat()
    path = ctx.reports_dir / f"weekly-{day}.md"
    path.write_text(text, encoding="utf-8")
    result.counts.update(posts=summary["posts"], file=path.name)
    ctx.notifier.notify(
        "주간 리포트", f"지난주 게시 {summary['posts']}건 결산이 준비됐습니다", "info"
    )
    return result
