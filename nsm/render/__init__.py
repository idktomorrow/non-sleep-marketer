"""카드 덱 → PNG 렌더러 (서브 담당, 배치 S-α).

엔진은 `get_renderer()`로 CardRenderer 포트 구현을 받는다. S-α가 머지되기 전에는
PlaceholderRenderer가 슬라이드마다 빈 PNG를 만들어 드라이런 흐름을 막지 않는다(process.md 배치 E 대체 규칙).
"""

from __future__ import annotations

from nsm.ports import CardRenderer


def get_renderer() -> CardRenderer:
    try:
        from nsm.render.html_renderer import HtmlCardRenderer  # type: ignore[import-not-found]
    except ImportError:
        from nsm.render.placeholder import PlaceholderRenderer

        return PlaceholderRenderer()
    return HtmlCardRenderer()
