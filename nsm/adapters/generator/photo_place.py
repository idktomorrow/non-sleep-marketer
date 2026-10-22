"""사진 짝짓기 요청 조립(S18 제안, ADR 0062): OpenAI 생성기의 place_photos가 쓴다(그림 입력 + strict 스키마)."""

from __future__ import annotations

import base64
import io

THUMB = 512
ESTIMATE_USD = 0.02
MAX_TOKENS = 3000

SCHEMA = {
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

SYSTEM = (
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


def thumb_data_url(path: str) -> str:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB, THUMB))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def user_content(cover_text: str, slides, photos) -> list[dict]:
    text = (
        "표지: "
        + cover_text[:200]
        + "\n장 목록:\n"
        + "\n".join(t for _, t in slides)
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
