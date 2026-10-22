"""실제 사진과 카드 장 짝짓기(S18 제안, ADR 0062): 순서가 아니라 **내용**으로 배치한다.

- 기사에서 모은 사진 후보(대표 이미지·본문 이미지, 설명 문구 포함)를 장 내용(제목·글·사진 힌트)과 맞춘다.
- OpenAI 글 생성기가 있으면 사진을 작게 줄여 직접 보여 주고 "이 장이 말하는 것을 실제로 보여 주는 사진"만 고르게 한다.
  없으면 사진 설명 문구와 장 글이 겹칠 때만 짝짓는다(겹침이 약하면 비워 둔다).
- 맞는 사진이 없는 장은 비운다. 한 사진은 한 장에만. 생성 그림으로 채우지 않는다.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from nsm.domain.models import CardDeck
from nsm.domain.quality import slide_texts

log = logging.getLogger(__name__)

THUMB = 512
ESTIMATE_USD = 0.02


@dataclass
class Placement:
    cover: int | None = None  # 후보 번호
    slides: dict[int, int] = field(default_factory=dict)  # 장 번호 → 후보 번호
    notes: dict[int, str] = field(default_factory=dict)  # 후보 번호 → 무엇을 보여 주는지(모델 판단)
    method: str = "none"  # vision | text | none


def _slide_brief(deck: CardDeck) -> list[tuple[int, str]]:
    out = []
    for i, s in enumerate(deck.slides):
        # 표지(0번)는 cover로 따로 고른다. 본문 장만 짝짓기 대상
        if i == 0 or s.kind in ("outro", "comic", "meme_image"):
            continue
        hint = getattr(s, "photo_hint", "") or ""
        text = " / ".join(slide_texts(s))[:220]
        out.append((i, f"[{i}] {s.kind}: {text}" + (f" (어울리는 사진: {hint})" if hint else "")))
    return out


def _thumb_data_url(path: str) -> str:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB, THUMB))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


_SCHEMA = {
    "type": "object",
    "properties": {
        "photos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"photo": {"type": "integer"}, "shows": {"type": "string"}},
                "required": ["photo", "shows"],
                "additionalProperties": False,
            },
        },
        "cover": {"type": "integer"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"slide": {"type": "integer"}, "photo": {"type": "integer"}},
                "required": ["slide", "photo"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["photos", "cover", "slides"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You place real news photos onto Korean card-news slides. First describe what each photo actually shows "
    "(product, poster, event scene, people, text in image). Then assign a photo to a slide ONLY when the photo "
    "shows what that slide talks about (the same product, the same event, the poster of that film, etc.). "
    "Never put a photo of a different product or event on a slide. Each photo may be used at most once. "
    "Order: FIRST assign body slides (a photo that shows exactly what one slide describes belongs to that slide), "
    "THEN choose the cover from the photos left over. "
    "cover: the photo that best represents the whole story's subject (the work, character, product or event the "
    "story is about — an official poster or key visual is ideal). The cover only needs to match the overall subject, "
    "not a specific detail; use -1 only when no photo shows that subject at all. "
    "Slides without a fitting photo get photo -1. It is fine to leave most slides without photos."
)


def _vision(gen, deck: CardDeck, cands: list[dict]) -> Placement | None:
    """OpenAI 생성기의 호출 경로(비용 원장·오류 분류)를 그대로 쓴다. 실패하면 None."""
    brief = _slide_brief(deck)
    content: list[dict] = [
        {
            "type": "input_text",
            "text": "표지: "
            + " / ".join(slide_texts(deck.slides[0]))[:200]
            + "\n장 목록:\n"
            + "\n".join(t for _, t in brief)
            + "\n\n사진 목록(번호 순서대로 이미지가 이어진다):\n"
            + "\n".join(
                f"[{k}] 출처 {c.get('source_url', '')[:80]} · 설명 {c.get('alt') or '없음'}"
                for k, c in enumerate(cands)
            ),
        }
    ]
    for c in cands:
        content.append(
            {"type": "input_image", "image_url": _thumb_data_url(c["path"]), "detail": "low"}
        )
    body = gen._body(_SYSTEM, "", _SCHEMA, "photo_placement", 3000)
    body["input"] = [{"role": "user", "content": content}]
    try:
        out = gen._parse(gen._text(gen._call("photo:match", body, ESTIMATE_USD)))
    except Exception as e:  # noqa: BLE001  배치는 있으면 좋고 없으면 비워 둔다
        log.info("사진 짝짓기 실패(비워 둠): %s", e)
        return None
    n = len(cands)
    valid = {i for i, _ in brief}
    p = Placement(method="vision")
    used: set[int] = set()
    cov = int(out.get("cover", -1))
    if 0 <= cov < n:
        p.cover = cov
        used.add(cov)
    for a in out.get("slides", []):
        si, ph = int(a["slide"]), int(a["photo"])
        if si in valid and 0 <= ph < n and ph not in used:
            p.slides[si] = ph
            used.add(ph)
    p.notes = {
        int(x["photo"]): str(x["shows"])[:120]
        for x in out.get("photos", [])
        if 0 <= int(x["photo"]) < n
    }
    return p


def _text_match(deck: CardDeck, cands: list[dict]) -> Placement:
    from nsm.domain.picks_policy import normalize_tokens

    p = Placement(method="text")
    og = next((k for k, c in enumerate(cands) if c.get("kind") == "og"), None)
    p.cover = og
    used = {og} if og is not None else set()
    for i, _t in _slide_brief(deck):
        s = deck.slides[i]
        want = normalize_tokens(" ".join([getattr(s, "photo_hint", "") or "", *slide_texts(s)]))
        best, score = None, 0
        for k, c in enumerate(cands):
            if k in used or not c.get("alt"):
                continue
            hit = len(want & normalize_tokens(c["alt"]))
            if hit > score:
                best, score = k, hit
        if best is not None and score >= 2:
            p.slides[i] = best
            used.add(best)
    return p


def match_photos(ctx, deck: CardDeck, cands: list[dict]) -> Placement:
    """돌려주는 번호는 받은 cands의 번호다(없는 파일은 빼고 맞춘 뒤 원래 번호로 되돌린다)."""
    keep = [k for k, c in enumerate(cands) if Path(c["path"]).is_file()]
    if not keep:
        return Placement()
    sub = [cands[k] for k in keep]
    from nsm.adapters.generator.openai_text import OpenAITextGenerator

    gen = ctx.text_generator
    p = _vision(gen, deck, sub) if isinstance(gen, OpenAITextGenerator) else None
    if p is None:
        p = _text_match(deck, sub)
    return Placement(
        cover=keep[p.cover] if p.cover is not None else None,
        slides={i: keep[k] for i, k in p.slides.items()},
        notes={keep[k]: v for k, v in p.notes.items()},
        method=p.method,
    )
