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
    """답 글을 안전한 HTML로: 전부 이스케이프 → 백틱 코드 → [n] 링크."""
    safe = html.escape(text or "", quote=True)
    safe = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", safe)

    def cite(m: re.Match) -> str:
        n = int(m.group(1))
        if n not in source_ns:
            return m.group(0)
        return f'<a class="cite" href="#src-{n}" data-src="{n}">[{n}]</a>'

    return Markup(_CITE.sub(cite, safe))  # noqa: S704  위에서 전부 이스케이프한 뒤 우리가 만든 태그만 넣었다
