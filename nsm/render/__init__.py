"""카드 덱 → PNG 렌더러 (배치 S-α, ADR 0015).

엔진은 `get_renderer()`로 CardRenderer 포트 구현을 받는다. 기본은 HtmlCardRenderer(템플릿 + Playwright).
Playwright나 크로미움 실행 파일이 없을 때만 PlaceholderRenderer(단색 PNG)로 대체해 흐름을 막지 않는다.
`nsm doctor`가 크로미움 미설치를 WARN으로 알린다.
"""

from __future__ import annotations

import importlib.util
import logging

from nsm.ports import CardRenderer

log = logging.getLogger(__name__)


def get_renderer() -> CardRenderer:
    if importlib.util.find_spec("playwright") is not None:
        from nsm.render.html_renderer import HtmlCardRenderer, chromium_available

        if chromium_available():
            return HtmlCardRenderer()
    from nsm.render.placeholder import PlaceholderRenderer

    log.warning(
        "크로미움이 없어 단색 카드로 대체합니다. `uv run playwright install chromium`을 실행하세요"
    )
    return PlaceholderRenderer()
