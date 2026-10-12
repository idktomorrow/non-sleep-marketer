"""카드 덱 JSON → HTML(Jinja2 템플릿) → PNG(Playwright 크로미움) 렌더러 (S3 #19).

- 템플릿: `nsm/templates/cards/{info,list,compare}.html` + `base.css`, 폰트는 `nsm/templates/fonts/`(Pretendard).
- 슬라이드 한 장 = 1080×1350. 한 페이지에 모든 슬라이드를 그려 놓고 슬라이드 요소마다 스크린샷을 찍는다.
- 글이 넘치면 템플릿의 `nsmFit()`이 `data-fit` 글자를 최소 크기까지 줄인다. 그래도 넘친 슬라이드 번호는
  `last_overflow`에 남는다(테스트·로그용).
- Playwright 동기 API는 스레드에 하나만 띄울 수 있다. 수집용 브라우저 드라이버가 같은 작업 스레드에서
  이미 떠 있을 수 있으므로, 렌더는 매번 전용 스레드에서 한다.
"""

from __future__ import annotations

import colorsys
import logging
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nsm.domain.models import CardDeck

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1350
TEMPLATES_DIR = Path(str(resources.files("nsm.templates")))


# ---------------------------------------------------------------- 색 계산


def _rgb(hex_color: str) -> tuple[float, float, float]:
    c = hex_color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0, min(1, v)) * 255):02X}" for v in rgb)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _on(rgb: tuple[float, float, float]) -> str:
    """배경 위 글자색: 흰색과 거의 검정 중 대비가 큰 쪽."""
    white, ink = (1.0, 1.0, 1.0), (0.07, 0.08, 0.1)
    return "#FFFFFF" if _contrast(rgb, white) >= _contrast(rgb, ink) else "#121419"


def _shade(rgb: tuple[float, float, float], light: float) -> tuple[float, float, float]:
    h, _l, s = colorsys.rgb_to_hls(*rgb)
    return colorsys.hls_to_rgb(h, light, s)


def _ink_on_light(rgb: tuple[float, float, float]) -> str:
    """밝은 면(#FAFAF7) 위에서 읽히는 브랜드 색: 너무 밝으면 어둡게."""
    surface = (0.98, 0.98, 0.97)
    out = rgb
    for light in (0.4, 0.32, 0.24, 0.16):
        if _contrast(out, surface) >= 4.5:
            break
        out = _shade(rgb, light)
    return _hex(out)


def palette(primary: str, accent: str) -> dict[str, str]:
    p, a = _rgb(primary), _rgb(accent)
    deep = tuple(v * 0.72 for v in p)
    card = tuple(0.94 + 0.06 * v for v in p)
    card_accent = tuple(0.92 + 0.08 * v for v in a)
    return {
        "primary": _hex(p),
        "primary_deep": _hex(deep),  # type: ignore[arg-type]
        "accent": _hex(a),
        "on_primary": _on(p),
        "on_accent": _on(a),
        "primary_ink": _ink_on_light(p),
        "accent_ink": _ink_on_light(a),
        "surface": "#FAFAF7",
        "ink": "#1B1D22",
        "card": _hex(card),  # type: ignore[arg-type]
        "card_accent": _hex(card_accent),  # type: ignore[arg-type]
        "badge_bg": "#1B1D22",
        "badge_fg": "#FFFFFF",
    }


# ---------------------------------------------------------------- HTML


def _file_uri(path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.resolve().as_uri() if p.exists() else ""


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["file_uri"] = _file_uri
    return env


def build_html(deck: CardDeck) -> str:
    """덱 → 한 페이지 HTML. 배경·로고 경로가 없으면 브랜드 색 그라디언트로 대체된다."""
    slides = []
    for s in deck.slides:
        bg = s.background if s.background and Path(s.background).exists() else None
        if s.background and not bg:
            log.warning("배경 이미지가 없어 브랜드 색으로 대체: %s", s.background)
        slides.append(s.model_copy(update={"background": bg}))
    logo = (
        deck.brand.logo_path
        if deck.brand.logo_path and Path(deck.brand.logo_path).exists()
        else None
    )
    deck = deck.model_copy(
        update={"slides": slides, "brand": deck.brand.model_copy(update={"logo_path": logo})}
    )
    return (
        _env()
        .get_template(f"cards/{deck.template}.html")
        .render(
            deck=deck,
            c=palette(deck.brand.primary_color, deck.brand.accent_color),
            css_uri=(TEMPLATES_DIR / "cards" / "base.css").resolve().as_uri(),
        )
    )


# ---------------------------------------------------------------- PNG


class HtmlCardRenderer:
    def __init__(self, timeout_ms: int = 30_000) -> None:
        self.timeout_ms = timeout_ms
        self.last_overflow: list[int] = []

    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        # 전용 스레드: 호출한 스레드에 Playwright(수집 드라이버)가 이미 떠 있어도 충돌하지 않는다
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="nsm-render") as pool:
            return pool.submit(self._render, deck, out_dir).result()

    def _render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        from playwright.sync_api import sync_playwright

        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / "deck.html"
        html_path.write_text(build_html(deck), encoding="utf-8")
        paths: list[Path] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
                )
                page.set_default_timeout(self.timeout_ms)
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")
                self.last_overflow = list(page.evaluate("window.nsmFit()"))
                if self.last_overflow:
                    log.warning("글이 넘친 슬라이드(최소 글자 크기에서도): %s", self.last_overflow)
                slides = page.locator("section.slide")
                for i in range(slides.count()):
                    path = out_dir / f"slide-{i + 1:02d}.png"
                    slides.nth(i).screenshot(path=str(path))
                    paths.append(path)
            finally:
                browser.close()
        return paths
