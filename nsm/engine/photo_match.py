"""실제 사진과 카드 장 짝짓기(S18 제안, ADR 0062): 순서가 아니라 **내용**으로 배치한다.

- 기사에서 모은 사진 후보(대표 이미지·본문 이미지, 설명 문구 포함)를 장 내용(제목·글·사진 힌트)과 맞춘다.
- OpenAI 글 생성기가 있으면 사진을 작게 줄여 직접 보여 주고 "이 장이 말하는 것을 실제로 보여 주는 사진"만 고르게 한다.
  없으면 사진 설명 문구와 장 글이 겹칠 때만 짝짓는다(겹침이 약하면 비워 둔다).
- 맞는 사진이 없는 장은 비운다. 한 사진은 한 장에만. 생성 그림으로 채우지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from nsm.domain.models import CardDeck
from nsm.domain.quality import slide_texts

log = logging.getLogger(__name__)


@dataclass
class Placement:
    cover: int | None = None  # 후보 번호
    slides: dict[int, int] = field(default_factory=dict)  # 장 번호 → 후보 번호
    items: dict[tuple[int, int], int] = field(
        default_factory=dict
    )  # (상품 장, 상품 번호) → 후보 번호
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


def _vision(placer, deck: CardDeck, cands: list[dict]) -> Placement | None:
    """그림을 볼 수 있는 공급자(포트 PhotoPlacer)에 묻고, 돌아온 번호를 검증한다. 실패하면 None."""
    brief = _slide_brief(deck)
    items = [
        f"[{i}.{j}] {it.name}" + (f" · {it.note}" if it.note else "")
        for i, s in enumerate(deck.slides)
        if s.kind == "products"
        for j, it in enumerate(s.items)
    ]
    try:
        out = placer.place_photos(
            " / ".join(slide_texts(deck.slides[0])),
            brief,
            [(c["path"], c.get("source_url", ""), c.get("alt", "")) for c in cands],
            items,
        )
    except Exception as e:  # noqa: BLE001  배치는 있으면 좋고 없으면 비워 둔다
        log.info("사진 짝짓기 실패(비워 둠): %s", e)
        return None
    try:
        return _validated(out, deck, brief, len(cands))
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        # 모양이 틀린 응답(번호 대신 None 등)은 버리고 글자 짝짓기로 넘어간다(S18 리뷰)
        log.info("사진 짝짓기 응답 형식 오류(글자 짝짓기로): %s", e)
        return None


def _validated(out: dict, deck: CardDeck, brief: list, n: int) -> Placement:
    valid = {i for i, _ in brief}
    p = Placement(method="vision")
    used: set[int] = set()
    cov = int(out.get("cover", -1))
    if 0 <= cov < n:
        p.cover = cov
        used.add(cov)
    valid_items = {
        (i, j)
        for i, s in enumerate(deck.slides)
        if s.kind == "products"
        for j in range(len(s.items))
    }
    # 상품 하나하나에 먼저(그 상품을 보여 주는 사진이 가장 구체적이다), 그다음 장
    for a in out.get("items", []):
        key, ph = (int(a["slide"]), int(a["item"])), int(a["photo"])
        if key in valid_items and 0 <= ph < n and ph not in used:
            p.items[key] = ph
            used.add(ph)
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
    from nsm.ports import PhotoPlacer

    gen = ctx.text_generator
    p = _vision(gen, deck, sub) if isinstance(gen, PhotoPlacer) else None
    if p is None:
        p = _text_match(deck, sub)
    return Placement(
        cover=keep[p.cover] if p.cover is not None else None,
        slides={i: keep[k] for i, k in p.slides.items()},
        items={key: keep[k] for key, k in p.items.items()},
        notes={keep[k]: v for k, v in p.notes.items()},
        method=p.method,
    )
