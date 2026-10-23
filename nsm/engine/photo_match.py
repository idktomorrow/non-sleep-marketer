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
    from nsm.adapters.generator.photo_place import USABLE_KINDS

    valid = {i for i, _ in brief}
    p = Placement(method="vision")
    # 남이 글자·막대를 얹어 만든 썸네일·배너·캡처는 어디에도 쓰지 않는다(사용자 피드백 2026-10-23: 파란 막대·겹친 제목)
    kinds = {int(x["photo"]): x.get("kind", "photo") for x in out.get("photos", [])}
    used: set[int] = {k for k in range(n) if kinds.get(k, "photo") not in USABLE_KINDS}
    cov = int(out.get("cover", -1))
    if 0 <= cov < n and cov not in used:
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
    # 표지는 사진이 있을 때 가장 좋다(사용자 피드백 2026-10-23): 남은 쓸 수 있는 사진, 없으면 본문 장의 사진을 표지로 옮긴다
    if p.cover is None:
        spare = [k for k in range(n) if k not in used]
        if spare:
            p.cover = spare[0]
        elif p.slides:
            si = min(p.slides)
            p.cover = p.slides.pop(si)
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


# ---------------------------------------------------------------- 관찰 → 원고 → 배치(S18 편집 밀도)


def _box(b) -> list[float] | None:
    """{x,y,w,h}(%) → [x, y, w, h], 사진 안으로 자르고 너무 작거나 틀린 상자는 버린다."""
    try:
        x, y, w, h = (float(b[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    x, y = max(0.0, min(x, 99.0)), max(0.0, min(y, 99.0))
    w, h = min(w, 100.0 - x), min(h, 100.0 - y)
    return [round(v, 1) for v in (x, y, w, h)] if w >= 3 and h >= 3 else None


def observe(ctx, rows: list[dict]) -> list:
    """원고를 쓰기 전에 실제 사진을 관찰한다(포트 PhotoObserver). 공급자가 없거나 실패하면 빈 목록(예전처럼 글만 보고 쓴다)."""
    from nsm.domain.models import ObservedPhoto, PhotoDetail
    from nsm.ports import PhotoObserver

    gen = ctx.text_generator
    keep = [r for r in rows if Path(r["path"]).is_file()]
    if not keep or not isinstance(gen, PhotoObserver):
        return []
    try:
        out = gen.observe_photos(
            [(r["path"], r.get("source_url", ""), r.get("alt", "")) for r in keep]
        )
    except Exception as e:  # noqa: BLE001  관찰은 있으면 좋고 없으면 글만 보고 쓴다
        log.info("사진 관찰 실패(글만 보고 씀): %s", e)
        return []
    by_k: dict[int, dict] = {}
    for x in out:
        try:
            by_k[int(x["photo"])] = x
        except (KeyError, TypeError, ValueError):
            continue
    photos = []
    for k, r in enumerate(keep):
        x = by_k.get(k, {})
        details = [
            PhotoDetail(what=str(d.get("what", ""))[:60], box=b)
            for d in (x.get("details") or [])[:3]
            if str(d.get("what", "")).strip() and (b := _box(d.get("box"))) is not None
        ]
        photos.append(
            ObservedPhoto(
                path=r["path"],
                source_url=r.get("source_url", ""),
                kind=str(x.get("kind", "photo")),
                shows=str(x.get("shows", ""))[:200] or r.get("alt", ""),
                details=details,
            )
        )
    return photos


def split_photos(deck: CardDeck, rows: list[dict]) -> tuple[CardDeck, dict | None]:
    """원고가 관찰한 사진을 골라 넣은 덱 → (사진을 뺀 덱, 배치). 사진은 사람이 "이 사진 쓰기"로 확인한 뒤에만
    카드에 들어간다(S11). 확대 영역·주석은 배치에 그대로 남겨 그때 적용한다."""
    index = {r["path"]: k for k, r in enumerate(rows)}
    placement: dict = {
        "cover": None,
        "slides": {},
        "items": {},
        "assets": {},
        "notes": {},
        "method": "writer",
    }
    slides = []
    found = False
    for i, s in enumerate(deck.slides):
        upd: dict = {}
        if i == 0 and s.background in index:
            placement["cover"] = index[s.background]
            upd["background"] = None
            found = True
        photo = getattr(s, "photo", None)
        if photo is not None and photo.path in index:
            placement["slides"][str(i)] = index[photo.path]
            placement["assets"][str(i)] = photo.model_dump(exclude={"path"})
            upd["photo"] = None
            found = True
        if s.kind == "products":
            items = list(s.items)
            for j, it in enumerate(items):
                if it.image in index:
                    placement["items"][f"{i}.{j}"] = index[it.image]
                    items[j] = it.model_copy(update={"image": None})
                    found = True
            upd["items"] = items
        slides.append(s.model_copy(update=upd) if upd else s)
    if not found:
        return deck, None
    return deck.model_copy(update={"slides": slides}), placement
