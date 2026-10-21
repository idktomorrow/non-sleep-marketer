"""밈 형식 탐색(S17 제안, ADR 0061): 재해석할 만한 유명·유행 밈 형식을 공개 페이지에서 찾는다.

- Know Your Meme 인기 목록(최근 확산 형식 앞쪽 몇 개) + 오래됐지만 유명한 형식 몇 개(항목 페이지를 직접 확인)
- 캐릿(careet.net) 밈 해설 목록에서 "유행 중" 표시된 국내 밈
- 항목마다 페이지에 **적힌** 설명·확산 기록(날짜·수치)만 근거로 남긴다. 추정·합산하지 않는다
- 대표 사례 그림(og:image)은 **밈 참조**로만 내려받는다(구도·텍스트 배치 이해용, 캐릭터 참조와 별도 폴더)
- X는 로그인 없이 볼 수 없어 쓰지 않는다. 출처 하나가 막혀도 나머지로 계속 간다
- 같은 날에는 결과를 캐시해(media/meme_formats/<날짜>/) 다시 요청하지 않는다
"""

from __future__ import annotations

import html as htmlmod
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from nsm.adapters.sources.common import Pacer
from nsm.adapters.sources.og_image import _default_resolver, _get, extract_og_image
from nsm.ports import Clock, MemeFormatCandidate

log = logging.getLogger(__name__)

KYM = "https://knowyourmeme.com"
POPULAR = KYM + "/memes/popular"
CAREET_LIST = "https://www.careet.net/Content/Series/1"
# 오래됐지만 유명한 형식(구도가 분명하고 캐릭터 재해석이 쉬운 것). 매번 항목 페이지를 열어 확인한다
CLASSIC = (
    "this-is-fine",
    "daily-struggle",
    "distracted-boyfriend",
    "drakeposting",
    "woman-yelling-at-a-cat",
    "surprised-pikachu",
)
SKIP = {"memes", "new", "popular", "submissions", "researching", "confirmed"}
MAX_HTML = 2 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MIN_SIDE = 150
UA = {"User-Agent": "Mozilla/5.0 (non-sleep-marketer meme-format research; local tool)"}


@dataclass
class FormatDiscovery:
    candidates: list[MemeFormatCandidate] = field(default_factory=list)
    access: dict[str, str] = field(default_factory=dict)


def _text(fragment: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


def _section(page: str, sid: str, limit: int) -> str:
    i = page.find(f'id="{sid}"')
    if i < 0:
        return ""
    i = page.find(">", i) + 1  # 여는 태그 뒤부터(속성 글자가 섞이지 않게)
    j = page.find("<h2", i)
    return _text(page[i : j if j > 0 else i + 20000])[:limit]


def _meta(page: str, prop: str) -> str:
    m = re.search(
        rf"<meta[^>]+(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]*>", page, flags=re.I
    )
    if not m:
        return ""
    c = re.search(r"content=[\"']([^\"']*)[\"']", m.group(0), flags=re.I)
    return htmlmod.unescape(c.group(1)).strip() if c else ""


class MemeFormatSource:
    def __init__(
        self,
        clock: Clock,
        pacer: Pacer,
        cache_root: Path,
        client: httpx.Client | None = None,
        resolver=_default_resolver,
        popular_n: int = 4,
        classic_n: int = 4,
        local_n: int = 2,
    ) -> None:
        self.clock = clock
        self.pacer = pacer
        self.cache_root = cache_root
        self.client = client or httpx.Client(timeout=15.0, follow_redirects=False, headers=UA)
        self.resolver = resolver
        self.popular_n, self.classic_n, self.local_n = popular_n, classic_n, local_n

    # ------------------------------------------------------------ 공용
    def _html(self, url: str) -> str:
        self.pacer.wait()
        _, body, _ = _get(self.client, url, MAX_HTML, self.resolver)
        return body.decode("utf-8", errors="replace")

    def _image(self, url: str, dest: Path) -> str:
        from PIL import Image

        if not url:
            return ""
        try:
            _, data, _ = _get(self.client, url, MAX_IMAGE, self.resolver)
            with Image.open(io.BytesIO(data)) as im:
                fmt, w, h = im.format, im.width, im.height
            if fmt not in ("JPEG", "PNG", "WEBP", "GIF") or min(w, h) < MIN_SIDE:
                return ""
            dest.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "GIF":  # 움짤은 첫 장만(구도 참고용)
                with Image.open(io.BytesIO(data)) as im:
                    im.convert("RGB").save(dest.with_suffix(".png"))
                return str(dest.with_suffix(".png"))
            ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}[fmt]
            dest.with_suffix(ext).write_bytes(data)
            return str(dest.with_suffix(ext))
        except Exception as e:  # noqa: BLE001  그림이 없어도 후보는 남긴다
            log.info("밈 참조 그림을 못 받음(%s): %s", url, e)
            return ""

    # ------------------------------------------------------------ Know Your Meme
    def _kym_entry(self, slug: str, era: str, folder: Path) -> MemeFormatCandidate | None:
        url = f"{KYM}/memes/{slug}"
        page = self._html(url)
        title = _meta(page, "og:title") or slug
        about = _section(page, "about", 700) or _meta(page, "og:description")
        origin = _section(page, "origin", 500)
        spread = _section(page, "spread", 900)
        image_url = _meta(page, "og:image") or extract_og_image(page, url) or ""
        image = self._image(image_url, folder / slug)
        return MemeFormatCandidate(
            key=slug,
            title=title.replace(" | Know Your Meme", "").strip(),
            url=url,
            image_path=image,
            image_url=image_url,
            era=era,
            evidence=(origin + " " + spread).strip(),
            about=about,
            site="Know Your Meme",
            checked_at=self.clock.now().isoformat(),
        )

    def _kym_popular(self) -> list[str]:
        page = self._html(POPULAR)
        slugs = []
        for s in re.findall(r'href="/memes/([a-z0-9\-]+)"', page):
            if s not in SKIP and s not in slugs:
                slugs.append(s)
        return slugs

    # ------------------------------------------------------------ 캐릿(국내)
    def _careet(self, folder: Path) -> list[MemeFormatCandidate]:
        page = self._html(CAREET_LIST)
        out: list[MemeFormatCandidate] = []
        hrefs = list(re.finditer(r'href="(/\d{3,5})"', page))
        seen: set[str] = set()
        for k, m in enumerate(hrefs):
            path = m.group(1)
            nxt = hrefs[k + 1].start() if k + 1 < len(hrefs) else m.end() + 3000
            block = _text(page[m.end() : nxt])
            if path in seen or "유행 중" not in block.replace("유행중", "유행 중"):
                continue
            seen.add(path)
            url = urljoin("https://www.careet.net", path)
            art = self._html(url)
            title = _meta(art, "og:title")
            desc = _meta(art, "og:description")
            body = _text(art)[:4000]
            image_url = _meta(art, "og:image")
            key = "careet-" + path.strip("/")
            out.append(
                MemeFormatCandidate(
                    key=key,
                    title=title,
                    url=url,
                    image_path=self._image(image_url, folder / key),
                    image_url=image_url,
                    era="local",
                    evidence="캐릿 목록에 '유행 중' 표시(조회·게시 수치는 페이지에 없음)",
                    about=(desc + " " + body[:700]).strip(),
                    site="캐릿",
                    checked_at=self.clock.now().isoformat(),
                )
            )
            if len(out) >= self.local_n:
                break
        return out

    # ------------------------------------------------------------ 진입점
    def discover(self) -> FormatDiscovery:
        today = self.clock.now().date().isoformat()
        folder = self.cache_root / today
        cache = folder / "formats.json"
        if cache.is_file():
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return FormatDiscovery(
                [MemeFormatCandidate(**c) for c in raw["candidates"]], raw["access"]
            )
        res = FormatDiscovery()
        res.access["X"] = "수집 안 함: 로그인·API 없이 볼 수 없음"
        slugs: list[tuple[str, str]] = []
        try:
            slugs += [(s, "recent") for s in self._kym_popular()[: self.popular_n]]
            res.access["Know Your Meme 인기"] = f"ok {len(slugs)}건"
        except Exception as e:  # noqa: BLE001
            res.access["Know Your Meme 인기"] = f"실패: {type(e).__name__}"
        slugs += [(s, "classic") for s in CLASSIC[: self.classic_n] if s not in dict(slugs)]
        got = 0
        for slug, era in slugs:
            try:
                c = self._kym_entry(slug, era, folder)
            except Exception as e:  # noqa: BLE001  항목 하나가 막혀도 계속
                log.info("밈 형식 항목 실패(%s): %s", slug, e)
                continue
            if c:
                res.candidates.append(c)
                got += 1
        res.access["Know Your Meme 항목"] = f"ok {got}/{len(slugs)}건"
        try:
            local = self._careet(folder)
            res.candidates += local
            res.access["캐릿(국내)"] = f"ok {len(local)}건"
        except Exception as e:  # noqa: BLE001
            res.access["캐릿(국내)"] = f"실패: {type(e).__name__}"
        if res.candidates:
            folder.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(
                    {"candidates": [asdict(c) for c in res.candidates], "access": res.access},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        return res
