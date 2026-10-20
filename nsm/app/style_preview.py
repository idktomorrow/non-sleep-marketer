"""설정 화면 카드 스타일 견본(S8): 지금 브랜드 색·페르소나로 스타일마다 표지·본문 장을 작게 그려 보여 준다.

- 색은 저장 전 화면 값(쿼리)으로도 그린다. 형식이 틀린 색은 거절한다(HTML에 그대로 들어가므로).
- 같은 스타일·색·페르소나·템플릿 버전이면 한 번 그린 PNG를 다시 쓴다(`media/style-previews/<지문>/`).
- 견본 폴더는 최근 것만 남긴다(색을 여러 번 바꿔 보면 쌓이므로).
"""

from __future__ import annotations

import functools
import hashlib
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from nsm.config.schema import CARD_STYLES, AppConfig
from nsm.domain.models import CardDeck
from nsm.render.html_renderer import TEMPLATES_DIR

HEX = re.compile(r"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})")
MAX_KEPT = 24
SLIDES = (1, 2)  # 표지, 본문(번호 카드)

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class StylePreviewError(ValueError):
    """잘못된 스타일·색·장 번호."""


# 사진 우선형(editorial, S10 제안) 견본에 까는 동봉 사진. 저작권 없는 합성 이미지(nsm/templates/samples/)
SAMPLE_PHOTO = TEMPLATES_DIR / "samples" / "sample-photo.jpg"


@functools.cache
def _photo_fingerprint() -> str:
    """견본 사진 파일을 바꾸면 editorial 견본도 다시 그리게(템플릿 지문은 html·css·woff2만 본다).

    파일은 업그레이드 때만 바뀌므로 프로세스당 한 번만 읽는다."""
    try:
        return hashlib.sha256(SAMPLE_PHOTO.read_bytes()).hexdigest()[:12]
    except OSError:
        return "nophoto"


def sample_deck(cfg: AppConfig, primary: str, accent: str, style: str = "clean") -> CardDeck:
    """브랜드와 상관없이 읽히는 견본 문구 + 지금 페르소나 이름·핸들·로고·표기.

    editorial은 사진이 있어야 모양이 드러나므로 표지·본문 장(화면에 보이는 1·2장)에 동봉 견본 사진을 깔고,
    본문은 설명 패널이 보이는 정보형으로 그린다(다른 스타일은 사진 없음, 리스트형).
    """
    logo = cfg.brand.logo_path
    if style == "editorial" and SAMPLE_PHOTO.is_file():
        photo, template = str(SAMPLE_PHOTO), "info"
        middle = {
            "kind": "body",
            "heading": "핵심만 정리",
            "body": "새로 나온 소식과 날짜·장소를 사진 위 패널에 짧게 적습니다.",
            "bullets": ["놓치면 아쉬운 점", "구매처와 시간"],
            "background": photo,
        }
    else:
        photo, template = None, "list"
        middle = {
            "kind": "list",
            "heading": "핵심만 정리",
            "bullets": ["새로 나온 소식", "날짜와 장소", "놓치면 아쉬운 점"],
        }
    return CardDeck.model_validate(
        {
            "template": template,
            "brand": {
                "name": cfg.brand.name,
                "primary_color": primary,
                "accent_color": accent,
                "logo_path": str(logo) if logo and Path(logo).exists() else None,
            },
            "persona": {"name": cfg.persona.name, "handle": cfg.persona.handle},
            "labels": {"virtual_person": True, "ad": cfg.persona.ad_label},
            "slides": [
                {
                    "kind": "cover",
                    "title": "이번 주 꼭 볼\n소식 3가지",
                    "subtitle": "카드 스타일 견본",
                    "background": photo,
                },
                middle,
                {"kind": "outro", "cta": "여러분은 어떤 게 궁금하세요?", "sources": ["견본"]},
            ],
        }
    )


def _lock_for(key: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _prune(root: Path, keep: Path) -> None:
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs[: max(0, len(dirs) - MAX_KEPT)]:
        if d != keep:
            shutil.rmtree(d, ignore_errors=True)


def style_preview_png(
    renderer: Any,
    cfg: AppConfig,
    media_dir: Path,
    style: str,
    n: int,
    primary: str = "",
    accent: str = "",
) -> Path:
    """스타일 견본 n번째 장 PNG 경로. 없으면 그린다(`renderer.render_sample`)."""
    if style not in CARD_STYLES:
        raise StylePreviewError(f"카드 스타일은 {', '.join(CARD_STYLES)} 중 하나입니다")
    if n not in SLIDES:
        raise StylePreviewError("견본은 1·2장만 있습니다")
    primary = primary or cfg.brand.primary_color
    accent = accent or cfg.brand.accent_color
    if not (HEX.fullmatch(primary) and HEX.fullmatch(accent)):
        raise StylePreviewError("색은 #RRGGBB 형식이어야 합니다")
    deck = sample_deck(cfg, primary.upper(), accent.upper(), style)
    version = str(getattr(renderer, "version", "")).split(":")[0]
    if style == "illust":  # 캐릭터를 바꾸면 일러스트형 견본도 다시 그린다(C17)
        art = getattr(renderer, "character", None)
        version += ":" + str(getattr(art, "fingerprint", ""))
    if style == "editorial":  # 견본 사진 파일을 바꾸면 사진 우선형 견본도 다시 그린다(S10)
        version += ":" + _photo_fingerprint()
    key = hashlib.sha256(f"{style}|{version}|{deck.model_dump_json()}".encode()).hexdigest()[:16]
    root = media_dir / "style-previews"
    out = root / key
    target = out / f"slide-{n:02d}.png"
    with _lock_for(key):
        if not target.exists():
            renderer.render_sample(deck, out, style)
            _prune(root, out)
    return target
