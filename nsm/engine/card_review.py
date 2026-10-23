"""완성 카드 검토(S18 프로세스): 미리보기로 그린 장을 보고, 목적에 맞게 보이는지와 문제의 원인 단계를 남긴다.

- 수치로 볼 수 있는 것(넘침, 사진을 몇 배 늘렸나)은 코드가 본다.
- 그림을 봐야 아는 것(핵심이 보이나, 잘림·가림, 글과 그림의 일치, 제목 줄)은 그림을 보는 모델(포트 CardReviewer)이 본다.
- 원인이 소재·기획(사진 고르기·장 역할)이거나 표지 배치면 검토 내용을 원고에 넘겨 **한 번만** 다시 편집한다.
  글 문제는 사람이 고친다. 글자를 줄여서 넘어가지 않는다.
"""

from __future__ import annotations

import json
import logging

from nsm.engine.context import JobContext

log = logging.getLogger(__name__)

TAG = "완성 카드 검토"
PART_LABEL = {"material": "소재", "plan": "기획", "layout": "배치", "text": "글"}
MAX_UPSCALE = 1.6


def _px(path: str | None) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:  # type: ignore[arg-type]
            return im.width, im.height
    except Exception:  # noqa: BLE001
        return 0, 0


def numeric_checks(deck, overflow: list[int]) -> list[str]:
    """수치로 확인되는 문제: 넘침, 사진을 1.6배 넘게 늘린 장."""
    out = [f"{n}장 글 넘침" for n in overflow]
    for i, s in enumerate(deck.slides, start=1):
        bg = getattr(s, "background", None)
        if bg:
            w, h = _px(bg)
            if w and max(1080 / w, 1350 / h) > MAX_UPSCALE:
                out.append(f"{i}장 배경 사진 {w}×{h}: 꽉 채우지 못해 작게 둠")
        photo = getattr(s, "photo", None)
        if photo is not None:
            w, _h = _px(photo.path)
            crop_w = (photo.crop[2] / 100) if photo.crop else 1.0
            if w and 904 / (w * crop_w) > MAX_UPSCALE:
                out.append(f"{i}장 사진 {w}px을 {904 / (w * crop_w):.1f}배로 늘림(흐릴 수 있음)")
    return out


def _brief(c, numeric: list[str]) -> str:
    g = c.generated
    notes = [n for n in (c.review_reason or "").split("; ") if n.startswith("관점:")]
    lines = [notes[0] if notes else "관점: (적힌 것 없음)", "장 목록:"]
    for i, s in enumerate(g.deck.slides, start=1):
        from nsm.domain.quality import slide_texts

        text = " / ".join(slide_texts(s))[:200]
        photo = getattr(s, "photo", None)
        pic = f" · 사진: {photo.alt[:60]}" if photo is not None and photo.alt else ""
        lines.append(f"[{i}] {s.kind}: {text}{pic}")
    if numeric:
        lines.append("코드가 잰 문제: " + "; ".join(numeric))
    return "\n".join(lines)


def run_card_review(ctx: JobContext, cid: str) -> str:
    from nsm.ports import CardReviewer

    gen = ctx.text_generator
    c = ctx.repo.get_content(cid)
    if c is None or c.generated.template != "editorial" or not isinstance(gen, CardReviewer):
        return "건너뜀"
    d = ctx.media_dir / "previews" / cid
    paths = sorted(d.glob("slide-*.png"))
    if not paths:
        return "미리보기 없음"
    of = d / "overflow.txt"
    overflow = (
        [int(x) for x in of.read_text().split(",") if x.strip().isdigit()] if of.is_file() else []
    )
    from nsm.engine.publish import labeled_deck

    numeric = numeric_checks(labeled_deck(c), overflow)
    try:
        out = gen.review_cards([str(p) for p in paths], _brief(c, numeric))
        problems = [
            (int(s["slide"]), str(p["part"]), str(p.get("note", "")).strip()[:120])
            for s in out.get("slides") or []
            for p in s.get("problems") or []
            if p.get("part") in PART_LABEL and str(p.get("note", "")).strip()
        ]
    except Exception as e:  # noqa: BLE001  검토는 있으면 좋고 없으면 사람이 본다
        log.info("완성 카드 검토 실패(넘어감): %s", e)
        return "검토 실패"
    found = [f"{n}장 {PART_LABEL[p]}: {note}" for n, p, note in problems][:6] + [
        f"수치: {x}" for x in numeric
    ][:4]
    kept = [x for x in (c.review_reason or "").split("; ") if x and not x.startswith(TAG)]
    ctx.repo.set_review_reason(cid, "; ".join([*kept, *(f"{TAG}: {x}" for x in found)]))
    # 원인이 소재·기획이거나 표지 배치면 한 번만 다시 편집(비용·반복 제한). 글 문제는 사람에게
    fix = [
        f"{n}장 {PART_LABEL[p]}: {note}"
        for n, p, note in problems
        if p in ("material", "plan") or (p == "layout" and n == 1)
    ]
    marker = ctx.media_dir / "picks" / cid / "auto-reedit.json"
    if fix and not marker.exists():
        from nsm.engine.picks import reedit_info

        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"feedback": fix}, ensure_ascii=False), encoding="utf-8")
        try:
            reedit_info(ctx, cid, feedback=fix)
        except Exception as e:  # noqa: BLE001
            log.info("검토 반영 다시 편집 실패: %s", e)
            return f"문제 {len(found)}건(다시 편집 실패)"
        return f"문제 {len(found)}건 → 검토 반영해 다시 편집(사진은 '이 사진 쓰기' 다시)"
    return f"문제 {len(found)}건" if found else "문제 없음"
