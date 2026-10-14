"""카드 덱 JSON → HTML(Jinja2 템플릿) → PNG(Playwright 크로미움) 렌더러 (S3 #19).

- 템플릿: `nsm/templates/cards/{info,list,compare}.html` + `base.css`, 폰트는 `nsm/templates/fonts/`(Pretendard).
- 슬라이드 한 장 = 1080×1350. 한 페이지에 모든 슬라이드를 그려 놓고 슬라이드 요소마다 스크린샷을 찍는다.
- 글이 넘치면 템플릿의 `nsmFit()`이 `data-fit` 글자를 최소 크기까지 줄인다. 그래도 넘친 슬라이드 번호는
  `last_overflow`에 남는다(테스트·로그용).
- Playwright 동기 API는 스레드에 하나만 띄울 수 있다. 수집용 브라우저 드라이버가 같은 작업 스레드에서
  이미 떠 있을 수 있으므로, 렌더는 전용 스레드 하나에서 하고 크로미움은 한 번 띄워 재사용한다.
"""

from __future__ import annotations

import atexit
import colorsys
import hashlib
import logging
import re
import tempfile
import threading
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


_WHITE, _INK = (1.0, 1.0, 1.0), (0.07, 0.08, 0.1)


def _on(*backgrounds: tuple[float, float, float]) -> str:
    """배경(여러 색이면 그라디언트 양 끝) 위 글자색: 가장 나쁜 쪽 대비가 더 큰 색."""
    w = min(_contrast(b, _WHITE) for b in backgrounds)
    k = min(_contrast(b, _INK) for b in backgrounds)
    return "#FFFFFF" if w >= k else "#121419"


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
    on_primary = _on(p, deep)  # type: ignore[arg-type]
    # 광고 배지는 법적 표기라 반드시 눈에 띄어야 한다: 강조색이 브랜드 면과 비슷하면 반전 색을 쓴다
    if min(_contrast(a, p), _contrast(a, deep)) >= 2.0:  # type: ignore[arg-type]
        ad_bg, ad_fg = _hex(a), _on(a)
    else:
        ad_bg = on_primary
        ad_fg = "#121419" if on_primary == "#FFFFFF" else "#FFFFFF"
    card = tuple(0.94 + 0.06 * v for v in p)
    card_accent = tuple(0.92 + 0.08 * v for v in a)
    return {
        "primary": _hex(p),
        "primary_deep": _hex(deep),  # type: ignore[arg-type]
        "accent": _hex(a),
        "on_primary": on_primary,
        "on_accent": _on(a),
        "primary_ink": _ink_on_light(p),
        "accent_ink": _ink_on_light(a),
        "surface": "#FAFAF7",
        "ink": "#1B1D22",
        "card": _hex(card),  # type: ignore[arg-type]
        "card_accent": _hex(card_accent),  # type: ignore[arg-type]
        "badge_bg": "#1B1D22",
        "badge_fg": "#FFFFFF",
        "ad_bg": ad_bg,
        "ad_fg": ad_fg,
    }


# ---------------------------------------------------------------- HTML


def _file_uri(path: str | Path | None) -> str:
    if not path:
        return ""
    p = Path(path)
    return p.resolve().as_uri() if p.exists() else ""


def _one_break(text: str | None) -> str:
    """줄 사이 빈 줄을 한 줄바꿈으로: 표지 제목에 빈 줄이 끼면 줄 사이가 크게 벌어진다(2026-10-13 실제 원고)."""
    return re.sub(r"\n\s*\n+", "\n", (text or "").strip())


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["file_uri"] = _file_uri
    env.filters["one_break"] = _one_break
    return env


def template_version() -> str:
    """카드 템플릿·CSS·폰트 파일 내용의 지문. 바뀌면 렌더 캐시를 버린다(publish.render_images)."""
    h = hashlib.sha256()
    for p in sorted(TEMPLATES_DIR.rglob("*")):
        if p.is_file() and p.suffix in {".html", ".css", ".woff2"}:
            h.update(p.relative_to(TEMPLATES_DIR).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


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
    """렌더 전용 스레드 하나에서 크로미움을 한 번 띄워 계속 쓴다.

    Playwright 객체는 만든 스레드에서만 쓸 수 있으므로 모든 렌더를 그 스레드로 보낸다.
    호출한 스레드에 다른 Playwright(수집 드라이버)가 떠 있어도 충돌하지 않는다.
    """

    def __init__(self, timeout_ms: int = 30_000) -> None:
        self.timeout_ms = timeout_ms
        self.last_overflow: list[int] = []
        self._pool: ThreadPoolExecutor | None = None
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()
        self.version = template_version()

    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        self.last_overflow = []  # 이전 덱의 값이 남지 않게
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nsm-render")
                atexit.register(self.close)
            pool = self._pool
        paths, overflow = pool.submit(self._render, deck, out_dir).result()
        self.last_overflow = overflow
        return paths

    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        return self._browser

    def _render(self, deck: CardDeck, out_dir: Path) -> tuple[list[Path], list[int]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        browser = self._ensure_browser()
        # 로컬 절대 경로가 담긴 HTML은 결과 폴더에 남기지 않는다(임시 폴더에서 렌더 후 삭제)
        with tempfile.TemporaryDirectory(prefix="nsm-card-") as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(build_html(deck), encoding="utf-8")
            page = browser.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            )
            try:
                page.set_default_timeout(self.timeout_ms)
                page.goto(html_path.resolve().as_uri(), wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")
                overflow = list(page.evaluate("window.nsmFit()"))
                if overflow:
                    log.warning("글이 넘친 슬라이드(최소 글자 크기에서도): %s", overflow)
                paths: list[Path] = []
                slides = page.locator("section.slide")
                for i in range(slides.count()):
                    path = out_dir / f"slide-{i + 1:02d}.png"
                    slides.nth(i).screenshot(path=str(path))
                    paths.append(path)
            finally:
                page.close()
        return paths, overflow

    def _shutdown(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        finally:
            self._browser = self._pw = None

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.submit(self._shutdown).result(timeout=30)
            except Exception:  # noqa: BLE001  종료 시점에는 최선만 다한다
                log.debug("렌더러 종료 중 오류", exc_info=True)
            pool.shutdown(wait=False)


def chromium_available() -> bool:
    """크로미움 실행 파일이 설치돼 있는지(`playwright install chromium`). 전용 스레드에서 확인."""

    def check() -> bool:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return bool(pool.submit(check).result(timeout=60))
    except Exception:  # noqa: BLE001
        return False
