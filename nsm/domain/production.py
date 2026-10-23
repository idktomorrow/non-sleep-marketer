"""그림 제작의 순수 규칙(S15 제안, ADR 0058): 출연진·제작 슬롯·슬롯별 spec과 지문·프롬프트 조립.

- **정체성 출처와 생성 여부는 다른 축이다.** `kind`(account 계정 캐릭터 | content 이 콘텐츠의 캐릭터 | custom 직접 지정)와
  `use`(generate 그림을 만든다 | real_only 실제 자료만, 생성 안 함). 콘텐츠 캐릭터(예: 치이카와)도 참조 이미지가 있으면
  밈의 생성 주인공이 될 수 있다. 실제 상품 사진은 원본으로 남고, 같은 캐릭터의 창작 만화는 별도 생성 자산이다.
- **유효성은 슬롯별 spec 지문**으로 판단한다(콘텐츠 판 번호가 아님). 지문에는 장면·출연 캐릭터(프로필 판)·참조 파일
  내용·구도·크기·품질이 들어가고 라벨·말풍선 글자는 들어가지 않는다 → 대사만 고쳐도 진행 중 그림은 그대로 적용된다.
- 2번째 컷은 1번째 컷 **결과 파일의 지문**을 참조로 포함한다(외형 일관성). 장면 복제가 아니라 "같은 캐릭터, 다른 장면"이다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from nsm.domain.models import CardDeck

Kind = Literal["account", "content", "custom"]
Use = Literal["generate", "real_only"]

PANEL_SIZE = (1536, 1024)
MEME_SIZE = (1024, 1280)  # 밈 한 장(S17): 4:5 세로 카드
STICKER_SIZE = (1024, 1024)
NEGATIVE = (
    "text, letters, numbers, captions, speech bubbles, logos, watermarks, signatures, "
    "real product packaging copies, other copyrighted characters"
)


def file_sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class CharacterRef:
    """출연 캐릭터 한 명. references는 **제작 당시 복사본**(원본 프로필 파일이 바뀌어도 이 원고는 그대로)."""

    kind: Kind
    use: Use = "generate"
    name: str = ""
    appearance: str = ""
    profile_id: str | None = None
    profile_revision: int = 0
    references: tuple[str, ...] = ()
    sources: tuple[
        str, ...
    ] = ()  # 참조 그림 출처(S17). 생성 → 편집 → 최종 산출물 표기까지 따라간다

    @property
    def refs_sha(self) -> str:
        h = hashlib.sha256()
        for r in self.references:
            p = Path(r)
            h.update(p.read_bytes() if p.is_file() else f"missing:{r}".encode())
        return h.hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["references"] = list(self.references)
        d["sources"] = list(self.sources)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> CharacterRef | None:
        if not d:
            return None
        return CharacterRef(
            kind=d.get("kind", "account"),
            use=d.get("use", "generate"),
            name=d.get("name", ""),
            appearance=d.get("appearance", ""),
            profile_id=d.get("profile_id"),
            profile_revision=int(d.get("profile_revision", 0)),
            references=tuple(d.get("references") or ()),
            sources=tuple(d.get("sources") or ()),
        )


@dataclass(frozen=True)
class Cast:
    hero: CharacterRef | None = None  # 만화 주인공
    sidekick: CharacterRef | None = None  # 정보 카드 보조 캐릭터(없음 가능)

    def to_json(self) -> str:
        return json.dumps(
            {
                "hero": self.hero.to_dict() if self.hero else None,
                "sidekick": self.sidekick.to_dict() if self.sidekick else None,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def from_json(raw: str | None) -> Cast:
        d = json.loads(raw) if raw else {}
        return Cast(
            CharacterRef.from_dict(d.get("hero")), CharacterRef.from_dict(d.get("sidekick"))
        )


@dataclass(frozen=True)
class SlotPlan:
    target: str  # "s1.panel0"
    role: Literal["panel", "sticker", "meme"]
    slide: int
    panel: int | None
    scene: str
    who: Literal["hero", "sidekick"]
    width: int
    height: int
    transparent: bool
    chain_from: str | None = None  # 1번째 컷 target(2번째 컷만)
    meme_ref: str | None = None  # 밈 참조 그림(구도·연출 이해용, 캐릭터 참조 아님, S17)
    text_zones: tuple[str, ...] = ()  # 글자가 들어갈 자리(그림에서 비워 둘 곳, S17)
    composite: bool = False  # 유명 짤 원본을 바탕으로 캐릭터만 합성(meme_ref가 바탕 그림)
    core: str = ""  # 재미의 핵심(기획의 core): 그림에서 꼭 살아 있어야 할 표정·시선·배치·동작


def _zone(o) -> str:
    where = (
        ("top" if o.y < 33 else "middle" if o.y < 66 else "bottom")
        + "-"
        + ("left" if o.x + o.w / 2 < 40 else "right" if o.x + o.w / 2 > 60 else "center")
    )
    return f"{o.kind} at {where} ({int(o.x)}%,{int(o.y)}%, width {int(o.w)}%)"


def image_slots(
    deck: CardDeck,
    cast: Cast,
    scenes: dict[str, str] | None = None,
    meme_ref: str | None = None,
    meme_make: str = "restage",
    meme_core: str = "",
) -> list[SlotPlan]:
    """덱에서 그림이 필요한 자리. 만화 컷은 주인공, 정보 카드는 첫 메모(body·facts) 한 곳에 보조 캐릭터 스티커,
    밈 한 장(S17)은 장 전체 그림 하나(캐릭터 참조 + 밈 참조)."""
    scenes = scenes or {}
    out: list[SlotPlan] = []
    sticker_done = False
    first_meme: str | None = None
    for i, s in enumerate(deck.slides):
        if s.kind == "meme_image":
            t = f"s{i}.image"
            chain = first_meme  # 여러 장 밈(S18): 2장째부터는 1장째 결과를 참조해 같은 모습으로
            first_meme = first_meme or t
            out.append(
                SlotPlan(
                    target=t,
                    role="meme",
                    slide=i,
                    panel=None,
                    scene=scenes.get(t) or s.scene,
                    who="hero",
                    width=MEME_SIZE[0],
                    height=MEME_SIZE[1],
                    transparent=False,
                    meme_ref=meme_ref if meme_ref and Path(meme_ref).is_file() else None,
                    text_zones=tuple(_zone(o) for o in s.overlays),
                    chain_from=chain,
                    composite=meme_make == "composite"
                    and bool(meme_ref and Path(meme_ref).is_file()),
                    core=meme_core,
                )
            )
        elif s.kind == "comic":
            for j, p in enumerate(s.panels):
                t = f"s{i}.panel{j}"
                out.append(
                    SlotPlan(
                        target=t,
                        role="panel",
                        slide=i,
                        panel=j,
                        scene=scenes.get(t) or p.scene,
                        who="hero",
                        width=PANEL_SIZE[0],
                        height=PANEL_SIZE[1],
                        transparent=False,
                        chain_from=f"s{i}.panel0" if j == 1 else None,
                    )
                )
        elif (
            not sticker_done
            and s.kind in ("body", "facts")
            and cast.sidekick is not None
            and cast.sidekick.use == "generate"
        ):
            t = f"s{i}.sticker"
            out.append(
                SlotPlan(
                    target=t,
                    role="sticker",
                    slide=i,
                    panel=None,
                    scene=scenes.get(t)
                    or "작은 반응 스티커: 메모를 보며 놀라거나 기뻐하는 표정, 전신, 가장자리 여백",
                    who="sidekick",
                    width=STICKER_SIZE[0],
                    height=STICKER_SIZE[1],
                    transparent=True,
                )
            )
            sticker_done = True
    return out


@dataclass(frozen=True)
class Spec:
    target: str
    role: str
    prompt: str
    width: int
    height: int
    transparent: bool
    quality: str | None
    references: tuple[str, ...]
    negative: str
    hash: str
    detail: dict[str, Any] = field(default_factory=dict)


class MissingReference(ValueError):
    """참조 이미지가 없는 캐릭터로 생성 요청. 다른 캐릭터로 조용히 대체하지 않는다."""


def compose_prompt(slot: SlotPlan, ref: CharacterRef, chained: bool) -> str:
    """결정적인 프롬프트(같은 입력 → 같은 문자열). 글자는 그림에 넣지 않는다(ADR 0007)."""
    who = ref.name or "the character"
    look = f" Appearance to keep exactly: {ref.appearance}." if ref.appearance else ""
    consistency = (
        " Keep the same character design as the reference images: face, colors, proportions, outfit."
        if ref.references
        else ""
    )
    if chained:
        consistency += (
            " The last reference is the previous panel: same character, but a DIFFERENT scene, "
            "background, pose and camera angle (do not copy the composition)."
        )
    if slot.role == "meme":
        must = (
            f" What makes it funny and MUST stay clearly visible: {slot.core}. The character must act inside the"
            " scene (natural size, gaze, contact and overlap), not float on top of it."
            if slot.core
            else ""
        )
        roles = must + (
            " Reference images: the FIRST image(s) are the CHARACTER reference — keep only the character's face, "
            "body shape, colors and proportions; ignore their background, pose, other characters and any text."
        )
        if slot.composite:
            # 유명 짤 원본에 캐릭터만 합성(사용자 피드백 2026-10-23): 원본 사진·인물·배경·구도는 그대로
            roles += (
                " The LAST image is the ORIGINAL famous meme picture: use it as the BASE image. Keep it as it is — "
                "the same photo, background, other people, framing, lighting and composition. Change ONLY what the "
                "scene says: replace that one person or object with our character (or insert our character there), "
                "same position, size and pose, drawn in the character's own cute illustration style and lit to sit "
                "naturally in the photo. Remove any text baked into the original image."
            )
            # 가로 원본이면 위아래를 빈 여백으로 채워 띠처럼 작아졌다(실호출) → 장면을 이어 그려 세로 칸을 꽉 채운다
            frame = (
                " Fill the whole vertical 4:5 frame edge to edge with the photo: if the original is wider, keep the"
                " main subjects large and extend the same scene naturally above and below (more of the street, sky,"
                " ground). No borders, no blank or solid-color margins, no letterboxing."
            )
            zones = (
                " Typeset text will be added later at: " + "; ".join(slot.text_zones) + "."
                if slot.text_zones
                else ""
            )
            return f"{who}. {slot.scene}.{look}{roles}{frame}{zones}"
        if slot.meme_ref:
            roles += (
                " The LAST image is the MEME FORMAT reference — use it only to understand the composition: "
                "panel layout, camera distance, framing, pose, gaze and expression contrast. Do NOT copy its people, "
                "characters, art style, text or logos; re-stage the scene with our character in its own world."
            )
        zones = (
            " Keep these areas calm and uncluttered for typeset text added later: "
            + "; ".join(slot.text_zones)
            + "."
            if slot.text_zones
            else ""
        )
        frame = (
            " Vertical 4:5 meme image, cute rounded illustration, clear readable expression at small size."
            " Keep the top-left strip (about 75% width, 10% height) and the bottom 6% free of faces, hands and key props"
            " (small labels sit there)."
        )
        return f"{who}. {slot.scene}.{look}{roles}{frame}{zones}"
    if slot.role == "panel":
        space = (
            " Leave the top-left corner calm and empty for a black caption box, and some empty space on one side "
            "for a speech bubble."
        )
        frame = " Wide 3:2 comic panel, cute rounded illustration style, clear facial expression, readable at small size."
    else:
        space = " Transparent background, the whole character visible with margin, no ground shadow box."
        frame = " Small die-cut sticker of one character, cute rounded illustration, thick clean outline."
    return f"{who}. Scene: {slot.scene}.{look}{consistency}{frame}{space}"


def build_spec(
    slot: SlotPlan,
    cast: Cast,
    quality: str | None,
    chain_image: str | None = None,
) -> Spec:
    ref = cast.hero if slot.who == "hero" else cast.sidekick
    if ref is None:
        raise MissingReference("출연 캐릭터가 정해지지 않았습니다")
    if ref.use != "generate":
        raise MissingReference(
            f"'{ref.name or ref.kind}'는 실제 자료만 쓰도록 정해져 있어 그림을 만들지 않습니다"
        )
    refs = [r for r in ref.references if Path(r).is_file()]
    if not refs:
        raise MissingReference(
            f"'{ref.name or ref.kind}' 캐릭터의 참조 이미지가 없습니다(설정 → 캐릭터 또는 이 원고 전용 참조)"
        )
    if chain_image:
        refs.append(chain_image)
    if slot.meme_ref:  # 밈 참조는 항상 마지막(프롬프트가 역할을 순서로 밝힌다)
        refs.append(slot.meme_ref)
    prompt = compose_prompt(slot, ref, chained=chain_image is not None)
    detail = {
        "target": slot.target,
        "role": slot.role,
        "scene": slot.scene,
        "who": slot.who,
        "character": {
            "kind": ref.kind,
            "profile_id": ref.profile_id,
            "profile_revision": ref.profile_revision,
            "appearance": ref.appearance,
        },
        "refs": [file_sha(r)[:16] for r in refs],
        "meme_ref": file_sha(slot.meme_ref)[:16] if slot.meme_ref else None,
        # 합성일 때만 넣는다(예전 그림의 지문이 바뀌지 않게). 합성 지시를 바꾸면 숫자를 올린다
        **({"composite": 2} if slot.composite else {}),
        **({"core": slot.core} if slot.core else {}),
        "text_zones": list(slot.text_zones),
        "size": [slot.width, slot.height],
        "transparent": slot.transparent,
        "quality": quality,
    }
    h = hashlib.sha256(json.dumps(detail, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[
        :24
    ]
    return Spec(
        target=slot.target,
        role=slot.role,
        prompt=prompt,
        width=slot.width,
        height=slot.height,
        transparent=slot.transparent,
        quality=quality,
        references=tuple(refs),
        negative=NEGATIVE,
        hash=h,
        detail=detail,
    )
