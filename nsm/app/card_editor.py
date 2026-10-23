"""카드 편집(M16 #35): 슬라이드 문구 고치기, 배경 이미지 올리기·바꾸기·지우기, 미리보기 렌더.

라우트는 server.py, 여기는 폼 → 덱 변환과 업로드 검사 같은 순수한 부분이다(테스트하기 쉽게).
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nsm.domain.models import CardDeck, GeneratedContent
from nsm.domain.quality import strip_bullet_marker

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB
# 파일 내용(앞 바이트)으로 형식을 판정한다. 이름·Content-Type은 믿지 않는다.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)

# 슬라이드 종류별로 고칠 수 있는 칸. 출처(outro.sources)는 parse_sources로 따로 검사한다.
TEXT_FIELDS = {
    "cover": ("title", "subtitle"),
    "body": ("heading", "body"),
    "list": ("heading",),
    "compare": ("heading",),
    "products": ("heading",),  # 항목은 s{i}_item{j}_name|price|note (S12)
    "facts": ("heading",),  # 줄은 s{i}_row{j}_label|value (S12)
    "outro": ("cta",),
}
LIST_FIELDS = {"body": ("bullets",), "list": ("bullets",)}
# 비우면 빈 카드가 게시된다(모델은 빈 문자열을 허용하므로 편집기에서 막는다)
REQUIRED_TEXT = {
    "cover": ("title",),
    "body": ("heading", "body"),
    "list": ("heading",),
    "compare": ("heading",),
    "products": ("heading",),
    "facts": ("heading",),
    "outro": ("cta",),
}


MAX_SOURCES = 5
MAX_SOURCE_CHARS = 200
# "javascript:", "file:", "ftp://" 처럼 http·https가 아닌 주소. "출처: 브랜드"처럼 콜론 뒤가 띄어진 이름은 통과.
_OTHER_SCHEME = re.compile(r"^(?!https?:)[a-z][a-z0-9+.-]*:\S", re.IGNORECASE)


class EditError(ValueError):
    """사람이 고친 값이 덱 계약(길이·필수값)에 맞지 않음. 400으로 돌려준다."""


def parse_sources(v: str) -> list[str]:
    """출처 칸(한 줄에 하나): 웹 주소(http·https) 또는 "브랜드 제공 자료" 같은 이름.

    SNS 글 끝 출처 줄과 마무리 카드에 그대로 나가므로 최소 1개, 다른 형식의 주소(javascript: 등)는 받지 않는다.
    """
    out = list(dict.fromkeys(x.strip() for x in str(v).splitlines() if x.strip()))
    if not out:
        raise EditError("출처는 1개 이상 있어야 합니다")
    if len(out) > MAX_SOURCES:
        raise EditError(f"출처는 {MAX_SOURCES}개까지입니다")
    for s in out:
        if len(s) > MAX_SOURCE_CHARS:
            raise EditError(f"출처 한 줄은 {MAX_SOURCE_CHARS}자까지입니다: {s[:40]}…")
        if _OTHER_SCHEME.match(s):
            raise EditError(f"웹 주소는 http·https만 쓸 수 있습니다: {s[:40]}")
    return out


def _lines(v: str) -> list[str]:
    return [strip_bullet_marker(x) for x in str(v).splitlines() if x.strip()]


def apply_slide_edits(deck: CardDeck, form: dict[str, Any]) -> CardDeck:
    """폼 값(s{번호}_{칸})을 덱에 반영한다. 폼에 없는 칸은 그대로 둔다(예전 폼과 호환)."""
    slides = []
    for i, s in enumerate(deck.slides):
        data = s.model_dump()
        for f in TEXT_FIELDS.get(s.kind, ()):
            key = f"s{i}_{f}"
            if key in form:
                data[f] = str(form[key]).strip()
        for f in LIST_FIELDS.get(s.kind, ()):
            key = f"s{i}_{f}"
            if key in form:
                data[f] = _lines(form[key])
        if s.kind == "outro" and f"s{i}_sources" in form:
            data["sources"] = parse_sources(form[f"s{i}_sources"])
        if s.kind == "compare":
            for side in ("left", "right"):
                if f"s{i}_{side}_title" in form:
                    data[side]["title"] = str(form[f"s{i}_{side}_title"]).strip()
                if f"s{i}_{side}_points" in form:
                    data[side]["points"] = _lines(form[f"s{i}_{side}_points"])
        if s.kind == "products":  # 항목 수는 그대로, 글자만(그림은 따로 올린다)
            for j, it in enumerate(data["items"]):
                for f in ("name", "price", "note"):
                    key = f"s{i}_item{j}_{f}"
                    if key in form:
                        it[f] = str(form[key]).strip()
                if not it["name"]:
                    raise EditError(f"{i + 1}장 상품 {j + 1}: 이름을 비워 둘 수 없습니다")
        if s.kind == "facts":
            for j, row in enumerate(data["rows"]):
                for f in ("label", "value"):
                    key = f"s{i}_row{j}_{f}"
                    if key in form:
                        row[f] = str(form[key]).strip()
                if not row["label"] or not row["value"]:
                    raise EditError(f"{i + 1}장 {j + 1}번째 줄: 항목과 내용이 모두 필요합니다")
        for f in REQUIRED_TEXT.get(s.kind, ()):
            if not str(data.get(f) or "").strip():
                raise EditError(f"{i + 1}장 {f}: 비워 둘 수 없습니다")
        if s.kind == "compare":
            for side in ("left", "right"):
                if not data[side]["title"].strip() or not data[side]["points"]:
                    raise EditError(f"{i + 1}장 {side}: 제목과 항목이 필요합니다")
        try:
            slides.append(type(s).model_validate(data))
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first.get("loc", ()))
            raise EditError(f"{i + 1}장 {where}: {first.get('msg', '형식 오류')}") from e
    update: dict[str, Any] = {"slides": slides}
    # 마무리 장이 없는 소식형(S18)은 덱 출처를 고친다(같은 검사, 캡션 출처 줄·마지막 장 작은 표기에 나간다)
    # 원래 출처가 없던 덱(자료 주소가 없는 원고)은 빈 칸 그대로 저장할 수 있다. 있던 출처를 다 지우는 것만 막는다(S18 리뷰)
    if "deck_sources" in form and (str(form["deck_sources"]).strip() or deck.sources):
        update["sources"] = parse_sources(form["deck_sources"])
    return deck.model_copy(update=update)


def drop_old_upload(media_dir: Path, cid: str, old: str | None) -> None:
    """바뀐(지워진) 배경이 사람이 올린 파일이면 지운다. 생성기 배경이나 다른 곳의 파일은 건드리지 않는다."""
    if not old:
        return
    p = Path(old).resolve()
    if p.parent == upload_dir(media_dir, cid).resolve() and p.is_file():
        p.unlink()


def sniff_image(head: bytes) -> str | None:
    """PNG·JPEG·WebP만 받는다. 맞으면 확장자, 아니면 None."""
    for sig, ext in _SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def upload_dir(media_dir: Path, cid: str) -> Path:
    return media_dir / "uploads" / "backgrounds" / cid


def save_upload(media_dir: Path, cid: str, index: int, data: bytes) -> Path:
    if len(data) > MAX_UPLOAD_BYTES:
        raise EditError("이미지는 10MB 이하만 올릴 수 있습니다")
    ext = sniff_image(data[:16])
    if ext is None:
        raise EditError("PNG, JPEG, WebP 이미지만 올릴 수 있습니다")
    out = upload_dir(media_dir, cid)
    out.mkdir(parents=True, exist_ok=True)
    # 파일 이름은 우리가 만든다(사용자 파일 이름을 경로에 쓰지 않음)
    path = out / f"slide-{index + 1:02d}-{secrets.token_hex(4)}{ext}"
    path.write_bytes(data)
    return path


def is_ai_background(path: str | None, media_dir: Path) -> bool:
    """이미지 생성기가 만든 배경(media/bg-*.png)인지. 사람이 올린 사진은 AI 생성이 아니다."""
    if not path:
        return False
    p = Path(path)
    return p.name.startswith("bg-") and p.parent.resolve() == media_dir.resolve()


def apply_photo_colors(gen: GeneratedContent, path: str | Path) -> GeneratedContent:
    """소재 사진에서 뽑은 색을 덱 브랜드 색에(S14: 포인트 색이 사진을 따라간다). 색을 못 뽑으면 그대로."""
    from nsm.render.colors import colors_from_photo

    colors = colors_from_photo(path)
    if colors is None:
        return gen
    primary, accent = colors
    brand = gen.deck.brand.model_copy(update={"primary_color": primary, "accent_color": accent})
    return gen.model_copy(update={"deck": gen.deck.model_copy(update={"brand": brand})})


def set_background(
    gen: GeneratedContent, index: int, path: str | None, media_dir: Path
) -> GeneratedContent:
    """슬라이드 배경을 바꾸고 "AI 생성 이미지" 표기를 다시 정한다.

    표기는 덱 전체에 하나뿐이라(동결 계약) AI 배경이 하나라도 남아 있으면 켠다. 사람이 올린 사진에도
    캡션이 붙을 수 있지만, 표기가 빠지는 것보다 안전하다(ADR 0024).
    """
    slides = list(gen.deck.slides)
    if not 0 <= index < len(slides):
        raise EditError("없는 슬라이드입니다")
    slides[index] = slides[index].model_copy(update={"background": path})
    ai = any(is_ai_background(s.background, media_dir) for s in slides)
    deck = gen.deck.model_copy(
        update={
            "slides": slides,
            "labels": gen.deck.labels.model_copy(update={"ai_generated_image": ai}),
        }
    )
    return gen.model_copy(update={"deck": deck})


def check_item_slot(gen: GeneratedContent, index: int, item: int) -> None:
    """상품 그림을 저장하기 전에 자리부터 확인한다(아니면 400을 돌려주고도 파일이 남는다, S12 리뷰)."""
    slides = gen.deck.slides
    if not 0 <= index < len(slides) or slides[index].kind != "products":
        raise EditError("상품 그리드 장이 아닙니다")
    if not 0 <= item < len(slides[index].items):
        raise EditError("없는 상품입니다")


def set_item_image(
    gen: GeneratedContent, index: int, item: int, path: str | None
) -> GeneratedContent:
    """상품 그리드(S12)의 j번째 상품 그림을 바꾼다. 사람이 올린 사진이라 "AI 생성 이미지" 표기와 무관하다."""
    check_item_slot(gen, index, item)
    slides = list(gen.deck.slides)
    items = list(slides[index].items)
    items[item] = items[item].model_copy(update={"image": path})
    slides[index] = slides[index].model_copy(update={"items": items})
    return gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})


def clear_rendered(media_dir: Path, cid: str, cards: bool = True) -> None:
    """고친 뒤에는 렌더된 카드(게시용)와 미리보기를 버린다. 다음 게시 때 다시 렌더한다."""
    dirs = [media_dir / "previews" / cid]
    if cards:
        dirs.append(media_dir / "cards" / cid)
    for d in dirs:
        for p in d.glob("slide-*.png"):
            p.unlink()
        (d / "overflow.txt").unlink(missing_ok=True)
