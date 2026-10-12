"""카드 덱 → PNG 렌더러 (배치 S-α, ADR 0015).

엔진은 `get_renderer()`로 CardRenderer 포트 구현을 받는다. 기본은 HtmlCardRenderer(템플릿 + Playwright).
Playwright가 설치돼 있지 않은 환경에서만 PlaceholderRenderer(단색 PNG)로 대체해 흐름을 막지 않는다.
"""

from __future__ import annotations

import importlib.util
import logging

from nsm.ports import CardRenderer

log = logging.getLogger(__name__)


def get_renderer() -> CardRenderer:
    if importlib.util.find_spec("playwright") is None:
        from nsm.render.placeholder import PlaceholderRenderer

        log.warning(
            "Playwright가 없어 단색 카드로 대체합니다(uv sync 후 playwright install chromium)"
        )
        return PlaceholderRenderer()
    from nsm.render.html_renderer import HtmlCardRenderer

    return HtmlCardRenderer()
