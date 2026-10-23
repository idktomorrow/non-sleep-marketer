"""사진 짝짓기 요청 조립(S18 제안, ADR 0062): OpenAI 생성기의 place_photos가 쓴다(그림 입력 + strict 스키마)."""

from __future__ import annotations

import base64
import io

THUMB = 512
# photo: 실물·현장 사진, official: 공식 포스터·키 비주얼·공식 상품 이미지(글자가 원래 작품의 일부),
# designed: 블로그·기사가 제목·막대·꼬리표·스티커를 얹어 만든 썸네일·배너, screenshot: 글자 목록·표·앱 화면 캡처
PHOTO_KINDS = ("photo", "official", "designed", "screenshot")
USABLE_KINDS = frozenset({"photo", "official"})
ESTIMATE_USD = 0.02
MAX_TOKENS = 3000

SCHEMA = {
    "type": "object",
    "properties": {
        "photos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "photo": {"type": "integer"},
                    "shows": {"type": "string"},
                    # 사진 종류(사용자 피드백 2026-10-23): 남이 글자·막대·꼬리표를 얹어 만든 썸네일·배너·캡처는 쓰지 않는다
                    "kind": {"type": "string", "enum": list(PHOTO_KINDS)},
                },
                "required": ["photo", "shows", "kind"],
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
        # 상품 장의 상품 하나하나(S18): "[3.1] B상 갑옷 인형" → slide 3, item 1
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slide": {"type": "integer"},
                    "item": {"type": "integer"},
                    "photo": {"type": "integer"},
                },
                "required": ["slide", "item", "photo"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["photos", "cover", "slides", "items"],
    "additionalProperties": False,
}

SYSTEM = (
    "You place real news photos onto Korean card-news slides. First describe what each photo actually shows "
    "(product, poster, event scene, people, text in image). Then assign a photo to a slide ONLY when the photo "
    "shows what that slide talks about (the same product, the same event, the poster of that film, etc.). "
    "Never put a photo of a different product or event on a slide. Each photo may be used at most once. "
    "Product lines like '[3.1] name' are individual products on a lineup slide: give each product the photo that "
    "shows THAT product (or a lineup sheet only if nothing better exists); never a different product. "
    "Order: FIRST assign products and body slides (a photo that shows exactly what one slide describes belongs to that slide), "
    "THEN choose the cover from the photos left over. "
    "cover: the photo that best represents the whole story's subject (the work, character, product or event the "
    "story is about — an official poster or key visual is ideal). The cover only needs to match the overall subject, "
    "not a specific detail; use -1 only when no photo shows that subject at all. "
    "Slides without a fitting photo get photo -1. It is fine to leave most slides without photos. "
    "Classify every photo's kind: 'photo' = a real photo of products/scene/people; 'official' = an official poster, "
    "key visual or official product image (its text is part of the original artwork); 'designed' = a thumbnail or "
    "banner that a blog or news site made by adding its own title text, bars, update tags or stickers on top; "
    "'screenshot' = a capture of text lists, tables, store lists or app screens. "
    "Use ONLY 'photo' and 'official' anywhere. Our card draws its own title, so a designed thumbnail would show "
    "two titles and foreign graphics. "
    "The cover is the most important slide: if any usable photo shows the story's subject, the cover gets one "
    "(prefer a clean product or scene photo with room for a title)."
)


def thumb_data_url(path: str) -> str:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB, THUMB))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def user_content(cover_text: str, slides, photos, items=()) -> list[dict]:
    text = (
        "표지: "
        + cover_text[:200]
        + "\n장 목록:\n"
        + "\n".join(t for _, t in slides)
        + ("\n상품 목록:\n" + "\n".join(items) if items else "")
        + "\n\n사진 목록(번호 순서대로 이미지가 이어진다):\n"
        + "\n".join(
            f"[{k}] 출처 {src[:80]} · 설명 {alt or '없음'}"
            for k, (_p, src, alt) in enumerate(photos)
        )
    )
    content: list[dict] = [{"type": "input_text", "text": text}]
    for path, _src, _alt in photos:
        content.append({"type": "input_image", "image_url": thumb_data_url(path), "detail": "low"})
    return content
