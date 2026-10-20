"""카드 덱 JSON → HTML(Jinja2 템플릿) → PNG(Playwright 크로미움) 렌더러 (S3 #19).

- 템플릿: `nsm/templates/cards/{info,list,compare}.html` + `base.css`, 폰트는 `nsm/templates/fonts/`(Pretendard).
- 슬라이드 한 장 = 1080×1350. 한 페이지에 모든 슬라이드를 그려 놓고 슬라이드 요소마다 스크린샷을 찍는다.
- 글이 넘치면 템플릿의 `nsmFit()`이 `data-fit` 글자를 최소 크기까지 줄인다. 그래도 넘친 슬라이드 번호는
  `last_overflow`에 남는다(테스트·로그용).
- 스타일(S8, ADR 0036 · S10 제안, ADR 0049): `clean`(기본)·`photo`·`illust`·`editorial`. `<body class="style-…">`와
  스타일별 장식(코드로 그린 도형·SVG 캐릭터, editorial은 사진 위 패널)만 바뀌고, 글자와 표기 슬롯은 모든 스타일이
  같은 HTML이다(글자를 이미지에 굽지 않는다).
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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nsm.config.schema import CARD_STYLES
from nsm.domain.models import CardDeck
from nsm.render.character import DRAWN, CharacterArt
from nsm.render.mascot import pick_prop

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


def _dark_under_white(
    rgb: tuple[float, float, float], min_contrast: float
) -> tuple[float, float, float]:
    """같은 색 계열에서 흰 글자 대비가 min_contrast 이상이 되는 가장 밝은 톤(노랑처럼 밝은 색도 어둡게)."""
    light = colorsys.rgb_to_hls(*rgb)[1]
    out = rgb
    while _contrast(out, _WHITE) < min_contrast and light > 0.02:
        light -= 0.02
        out = _shade(rgb, light)
    return out


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
    # photo 스타일 바탕: 주 색을 아주 어둡게(흰 글자가 늘 읽히게). 광고 배지는 이 바탕에서도 눈에 띄어야 한다
    photo_dark = min(_shade(p, 0.12), _dark_under_white(p, 14.0), key=_luminance)
    photo_mid = min(_shade(p, 0.26), _dark_under_white(p, 8.0), key=_luminance)
    if _contrast(a, photo_mid) >= 2.0:
        photo_ad_bg, photo_ad_fg = _hex(a), _on(a)
    else:
        photo_ad_bg, photo_ad_fg = "#FFFFFF", "#121419"
    # illust 스타일 바탕: 주 색을 아주 옅게(파스텔). 글자는 ink, 광고 배지는 파스텔 위에서 보이는 색
    illust_bg = _shade(p, 0.94)
    if _contrast(a, illust_bg) >= 2.0:
        illust_ad_bg, illust_ad_fg = _hex(a), _on(a)
    else:
        illust_ad_bg, illust_ad_fg = "#1B1D22", "#FFFFFF"
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
        "photo_dark": _hex(photo_dark),
        "photo_mid": _hex(photo_mid),
        # 배경 원 색: 주 색 계열이되 밝기를 고정(밝은 브랜드 색이어도 흰 글자가 읽히게)
        "photo_glow": _hex(min(_shade(p, 0.34), _dark_under_white(p, 5.0), key=_luminance)),
        "photo_ad_bg": photo_ad_bg,
        "photo_ad_fg": photo_ad_fg,
        "illust_bg": _hex(illust_bg),
        "illust_ground": _hex(_shade(p, 0.84)),
        "illust_ad_bg": illust_ad_bg,
        "illust_ad_fg": illust_ad_fg,
        # 캐릭터 몸 색: 주 색의 밝은 톤(채도는 그대로). 진한 눈·입이 늘 보이게
        "mascot_body": _hex(_shade(p, 0.7)),
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


def check_style(style: str) -> str:
    if style not in CARD_STYLES:
        raise ValueError(f"카드 스타일은 {', '.join(CARD_STYLES)} 중 하나입니다: {style}")
    return style


# 저장소 루트(nsm 패키지의 부모). 픽스처처럼 저장소 기준 상대 경로로 적힌 그림을 어느 폴더에서 실행해도 찾게
PACKAGE_ROOT = TEMPLATES_DIR.parent.parent


def resolve_asset(path: str | None) -> str | None:
    """그림 경로: 절대 경로나 현재 폴더 기준으로 있으면 그것, 없으면 저장소 루트 기준으로 한 번 더 찾는다(S10 리뷰)."""
    if not path:
        return None
    p = Path(path)
    if p.exists():
        return str(p)
    if not p.is_absolute() and (PACKAGE_ROOT / p).exists():
        return str(PACKAGE_ROOT / p)
    return None


def _deck_texts(deck: CardDeck) -> list[str]:
    out: list[str] = []
    for s in deck.slides:
        for key in ("title", "subtitle", "heading", "body", "cta"):
            v = getattr(s, key, None)
            if isinstance(v, str):
                out.append(v)
        out.extend(getattr(s, "bullets", None) or [])
    return out


def build_html(deck: CardDeck, style: str = "clean", character: CharacterArt = DRAWN) -> str:
    """덱 → 한 페이지 HTML. 배경·로고 경로가 없으면 스타일의 코드 배경(그라디언트·도형)으로 대체된다.

    `character`는 일러스트형 표지·마지막 장의 캐릭터(C17). 다른 스타일에서는 쓰지 않는다.
    """
    check_style(style)
    slides = []
    for s in deck.slides:
        bg = resolve_asset(s.background)
        if s.background and not bg:
            log.warning("배경 이미지가 없어 브랜드 색으로 대체: %s", s.background)
        update: dict = {"background": bg}
        if s.kind == "products":  # 상품 그림도 같은 규칙(S12): 없으면 이름만 크게
            update["items"] = [
                it.model_copy(update={"image": resolve_asset(it.image)}) for it in s.items
            ]
        slides.append(s.model_copy(update=update))
    logo = resolve_asset(deck.brand.logo_path)
    deck = deck.model_copy(
        update={"slides": slides, "brand": deck.brand.model_copy(update={"logo_path": logo})}
    )
    return (
        _env()
        .get_template(f"cards/{deck.template}.html")
        .render(
            deck=deck,
            c=palette(deck.brand.primary_color, deck.brand.accent_color),
            style=style,
            character=character,
            # 사진 우선형의 몽글 캐릭터 소품: 카드 글에서 고른다(항공→조종사 모자, 커피→머그컵 …)
            prop=pick_prop(_deck_texts(deck)),
            css_uri=(TEMPLATES_DIR / "cards" / "base.css").resolve().as_uri(),
        )
    )


# ---------------------------------------------------------------- PNG


class HtmlCardRenderer:
    """렌더 전용 스레드 하나에서 크로미움을 한 번 띄워 계속 쓴다.

    Playwright 객체는 만든 스레드에서만 쓸 수 있으므로 모든 렌더를 그 스레드로 보낸다.
    호출한 스레드에 다른 Playwright(수집 드라이버)가 떠 있어도 충돌하지 않는다.

    `style`은 문자열이나 함수다. 함수면 렌더할 때마다 불러서, 대시보드에서 바꾼 `brand.card_style`이
    재시작 없이 다음 렌더부터 적용된다(포트 `render(deck, out_dir)`는 동결이라 스타일을 인자로 받지 않는다).
    """

    def __init__(
        self,
        timeout_ms: int = 30_000,
        style: str | Callable[[], str] = "clean",
        character: CharacterArt | Callable[[], CharacterArt] = DRAWN,
    ) -> None:
        self.timeout_ms = timeout_ms
        self.last_overflow: list[int] = []
        self._pool: ThreadPoolExecutor | None = None
        self._pw = None
        self._browser = None
        self._lock = threading.Lock()
        self._style = style
        self._character = character
        self._template_version = template_version()

    @property
    def style(self) -> str:
        return check_style(self._style() if callable(self._style) else self._style)

    @property
    def character(self) -> CharacterArt:
        return self._character() if callable(self._character) else self._character

    @property
    def version(self) -> str:
        """렌더 캐시 지문(publish.render_images): 템플릿 파일·스타일·캐릭터 중 하나라도 바뀌면 다시 그린다.

        캐릭터는 일러스트형에서만 그리므로 그때만 지문에 넣는다(다른 스타일 카드는 다시 그리지 않게)."""
        tail = f":{self.character.fingerprint}" if self.style == "illust" else ""
        return f"{self._template_version}:{self.style}{tail}"

    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]:
        self.last_overflow = []  # 이전 덱의 값이 남지 않게
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nsm-render")
                atexit.register(self.close)
            pool = self._pool
        paths, overflow = pool.submit(
            self._render, deck, out_dir, self.style, self.character
        ).result()
        self.last_overflow = overflow
        return paths

    def render_sample(self, deck: CardDeck, out_dir: Path, style: str) -> list[Path]:
        """설정 화면 스타일 견본: 지정한 스타일로 그린다. 게시용 렌더의 `last_overflow`는 건드리지 않는다
        (게시 잡이 렌더 직후 읽는 값이 요청 스레드의 견본 렌더로 바뀌면 안 된다)."""
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nsm-render")
                atexit.register(self.close)
            pool = self._pool
        paths, _overflow = pool.submit(
            self._render, deck, out_dir, check_style(style), self.character
        ).result()
        return paths

    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        return self._browser

    def _render(
        self, deck: CardDeck, out_dir: Path, style: str, character: CharacterArt = DRAWN
    ) -> tuple[list[Path], list[int]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        browser = self._ensure_browser()
        # 로컬 절대 경로가 담긴 HTML은 결과 폴더에 남기지 않는다(임시 폴더에서 렌더 후 삭제)
        with tempfile.TemporaryDirectory(prefix="nsm-card-") as tmp:
            html_path = Path(tmp) / "deck.html"
            html_path.write_text(build_html(deck, style, character), encoding="utf-8")
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
