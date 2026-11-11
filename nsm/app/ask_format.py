"""문서 질문 화면 표시(R3c): 걸린 시간·비용·답 글.

답 글은 Claude가 쓴 것이라 그대로 HTML로 넣지 않는다. 먼저 전부 이스케이프하고, 그 위에 두 가지만 바꾼다.
- 백틱 한 쌍(`…`) → <code>…</code> (마크다운 전체 렌더링은 하지 않는다)
- [n] → 근거 n으로 가는 링크(근거 목록에 있는 번호만, 없으면 글자 그대로)
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_CODE = re.compile(r"`([^`\n]{1,200})`")
_CITE = re.compile(r"\[(\d{1,2})\]")


def seconds(ms: int | None) -> str:
    """4733 → "4.7초"."""
    return f"{(ms or 0) / 1000:.1f}초"


def cost(usd: float | None, krw_per_usd: float) -> str:
    """0.000353 → "$0.0004 (약 0.5원)". 0이면 "$0 (0원)"."""
    usd = usd or 0.0
    if usd <= 0:
        return "$0 (0원)"
    krw = usd * krw_per_usd
    won = f"{krw:.1f}" if krw < 10 else f"{krw:,.0f}"
    return f"${usd:.4f} (약 {won}원)"


def answer_html(text: str | None, source_ns: set[int]) -> Markup:
    """답 글을 안전한 HTML로: 전부 이스케이프 → 백틱 코드 → (코드 밖의) [n] 링크."""
    safe = html.escape(text or "", quote=True)

    def cite(m: re.Match) -> str:
        n = int(m.group(1))
        if n not in source_ns:
            return m.group(0)
        return f'<a class="cite" href="#src-{n}" data-src="{n}">[{n}]</a>'

    out: list[str] = []
    pos = 0
    for m in _CODE.finditer(safe):  # 코드 사이의 글에만 [n] 링크(코드 안의 arr[1]은 그대로)
        out.append(_CITE.sub(cite, safe[pos : m.start()]))
        out.append(f"<code>{m.group(1)}</code>")
        pos = m.end()
    out.append(_CITE.sub(cite, safe[pos:]))
    return Markup("".join(out))  # noqa: S704  위에서 전부 이스케이프한 뒤 우리가 만든 태그만 넣었다


def estimate_won(model: str, krw_per_usd: float) -> str:
    """입력 칸 안내의 질문당 Claude 비용 어림(실측 평균: 입력 약 2,000·출력 약 250토큰)."""
    from nsm.adapters.pricing import claude_cost

    krw = claude_cost(model, 2000, 250) * krw_per_usd
    return f"약 {krw:.1f}원" if krw < 10 else f"약 {krw:,.0f}원"


# 우리 글 근거의 "글 보기" 링크 순서와 이름(R4d). 모르는 플랫폼은 뒤에 이름 그대로
_LINK_ORDER = ("threads", "instagram")
_LINK_NAME = {"threads": "스레드", "instagram": "인스타그램", "naver_blog": "네이버 블로그"}


def post_links(links: dict[str, str] | None) -> list[tuple[str, str]]:
    """근거의 `links`(플랫폼 → 주소)를 화면용 [(이름, 주소)]로. http·https 주소만 둔다.

    주소는 게시 어댑터가 받아 온 값이지만, 다른 스킴(javascript: 등)이 섞여도 링크로 만들지 않는다.
    이스케이프는 템플릿 자동 이스케이프에 맡긴다.
    """
    if not isinstance(links, dict):  # 기록 JSON이 다른 모양이어도 화면이 깨지지 않게
        return []
    items = sorted(
        links.items(),
        key=lambda kv: (
            _LINK_ORDER.index(kv[0]) if kv[0] in _LINK_ORDER else len(_LINK_ORDER),
            kv[0],
        ),
    )
    return [
        (f"{_LINK_NAME.get(p, p)} 글 보기", u)
        for p, u in items
        if isinstance(u, str) and u.lower().startswith(("https://", "http://"))
    ]


def post_metrics(metrics: dict[str, dict] | None, tz: str) -> list[str]:
    """근거의 `metrics`(R5) → 화면 줄 ["스레드 조회 1,234 · 좋아요 12 · 댓글 3 · 공유 1 (11-11 기준)"].

    천 단위 쉼표, 없는 값은 뺀다(저장은 인스타그램만). 값이 하나도 없는 플랫폼은 줄을 만들지 않는다.
    프롬프트의 성과 줄(`answer.metrics_note`)과 같은 `metric_parts`를 쓴다(참여율은 화면에 넣지 않음).
    """
    from nsm.rag.answer import PLATFORM_KO, _day, metric_parts, ordered_platforms

    if not isinstance(metrics, dict):
        return []
    out = []
    for platform in ordered_platforms(metrics):
        m = metrics[platform]
        if not isinstance(m, dict):
            continue
        parts = metric_parts(platform, m, with_rate=False)
        if not parts:
            continue
        try:
            day = _day(m.get("captured_at"), tz)
        except (TypeError, ValueError):
            day = ""
        when = f" ({day} 기준)" if day else ""
        out.append(f"{PLATFORM_KO.get(platform, platform)} {' · '.join(parts)}{when}")
    return out
