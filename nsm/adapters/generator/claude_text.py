"""Claude 텍스트 생성기: 카드뉴스 원고·캡션·스레드 글·블로그 글·대댓글.

- 구조화 출력(JSON 스키마)으로 받는다. 카드 덱의 판별 유니온은 스키마로 표현하기 까다로워서,
  평평한 LLM 출력(`_LLM_SCHEMA`)을 받아 코드에서 `CardDeck`으로 조립한다.
- 사실 주장(claims)마다 입력 자료 ID를 달게 하고, 정책이 그 ID가 실제로 있는지 검사한다(ADR 0008).
- Sonnet 5.5는 안전 분류기가 요청을 거절할 수 있어 server-side fallback(`fallbacks: "default"`)을 켠다.
"""

from __future__ import annotations

import json
from typing import Any

from nsm.adapters.pricing import claude_cost
from nsm.domain.models import (
    BodySlide,
    Brand,
    CardDeck,
    Claim,
    Comment,
    CompareSide,
    CompareSlide,
    ContentBrief,
    CoverSlide,
    DeckBrand,
    DeckLabels,
    DeckPersona,
    GeneratedContent,
    ListSlide,
    OutroSlide,
    Persona,
    SourceItem,
)
from nsm.ports import AdapterError, CostSink

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_MIDDLE_SLIDES = 8

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": _STR,
        "cover_title": _STR,
        "cover_subtitle": _STR,
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": _STR,
                    "body": _STR,
                    "bullets": _STRS,
                    "left_title": _STR,
                    "left_points": _STRS,
                    "right_title": _STR,
                    "right_points": _STRS,
                },
                "required": [
                    "heading",
                    "body",
                    "bullets",
                    "left_title",
                    "left_points",
                    "right_title",
                    "right_points",
                ],
                "additionalProperties": False,
            },
        },
        "cta": _STR,
        "instagram_caption": _STR,
        "threads_text": _STR,
        "blog_title": _STR,
        "blog_markdown": _STR,
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": _STR, "source_item_id": _STR},
                "required": ["text", "source_item_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "topic",
        "cover_title",
        "cover_subtitle",
        "slides",
        "cta",
        "instagram_caption",
        "threads_text",
        "blog_title",
        "blog_markdown",
        "claims",
    ],
    "additionalProperties": False,
}

_TEMPLATE_GUIDE = {
    "info": "정보형: slides 2~5장. 각 장은 heading(15자 이내)과 body(90자 이내). bullets는 0~3개. left/right 필드는 빈 값.",
    "list": "리스트형: slides 1~3장. 각 장은 heading과 bullets 3~5개(각 25자 이내). body와 left/right 필드는 빈 값.",
    "compare": "비교형: slides 1~2장. 각 장은 heading과 left_title·left_points(2~4개), right_title·right_points(2~4개). body·bullets는 빈 값.",
}


def _system(persona: Persona, brand: Brand) -> str:
    return f"""당신은 한국어 SNS 콘텐츠를 쓰는 AI 가상 인플루언서 '{persona.name}'({persona.handle})입니다.
인물상: {persona.character}
말투: {persona.tone}
브랜드: {brand.name} ({brand.industry}). 제품: {", ".join(brand.products) or "-"}. 핵심 메시지: {", ".join(brand.key_messages) or "-"}.

규칙:
- 사실은 주어진 <source> 자료에서만 가져옵니다. 사실 주장 하나마다 claims에 그 문장과 근거 자료의 id를 넣습니다. 자료에 없는 수치·효능·사실은 쓰지 않습니다.
- 원문 문장을 20자 넘게 그대로 옮기지 않고, 사실만 자기 말로 요약합니다.
- 당신은 가상인물이므로 직접 사용·섭취·체험한 것처럼 쓰지 않습니다("써봤는데", "먹어보니", "내돈내산", "사용 후기" 금지). 실제 고객 후기 자료는 "고객 후기에 따르면"처럼 인용만 합니다.
- 금칙어를 쓰지 않습니다: {", ".join(persona.banned_words) or "-"}.
- "AI 가상인물", "광고" 같은 표기 문구는 시스템이 자동으로 붙이므로 직접 쓰지 않습니다.
- instagram_caption: 300~800자, 줄바꿈으로 읽기 쉽게, 해시태그 3~5개를 끝에.
- threads_text: 500자 이내의 짧은 글.
- blog_title: 40자 이내. blog_markdown: 1500~2500자 마크다운, ## 소제목 3개 이상, 끝에 '참고 자료' 목록.
- cover_title: 두 줄 이내(줄당 14자 내외, 줄바꿈은 \\n). cta: 20자 이내."""


def _user(brief: ContentBrief) -> str:
    sources = "\n".join(
        f'<source id="{s.id}" title="{s.title}" url="{s.url or ""}">\n{s.text[:3000]}\n</source>'
        for s in brief.sources
    )
    return (
        f"주제 방향: {brief.topic}\n템플릿: {brief.template} — {_TEMPLATE_GUIDE[brief.template]}\n"
        f"협찬 게시물: {'예' if brief.is_ad else '아니오'}\n\n자료:\n{sources}"
    )


def _display_source(item: SourceItem) -> str:
    if not item.url or item.url.startswith("file:"):
        return "브랜드 제공 자료"
    return item.url


def build_deck(
    template: str, out: dict[str, Any], brand: Brand, persona: Persona, brief: ContentBrief
) -> CardDeck:
    middle: list[Any] = []
    for s in out["slides"][:MAX_MIDDLE_SLIDES]:
        heading = s["heading"].strip()
        if template == "info" and heading and s["body"].strip():
            middle.append(
                BodySlide(heading=heading, body=s["body"].strip(), bullets=s["bullets"][:3])
            )
        elif template == "list" and heading and s["bullets"]:
            middle.append(ListSlide(heading=heading, bullets=s["bullets"][:5]))
        elif template == "compare" and s["left_points"] and s["right_points"]:
            middle.append(
                CompareSlide(
                    heading=heading or "비교",
                    left=CompareSide(title=s["left_title"] or "A", points=s["left_points"][:4]),
                    right=CompareSide(title=s["right_title"] or "B", points=s["right_points"][:4]),
                )
            )
    if not middle:
        raise AdapterError(f"생성 결과에 {template} 템플릿에 맞는 슬라이드가 없습니다")
    used = {c["source_item_id"] for c in out["claims"]}
    sources = list(
        dict.fromkeys(_display_source(i) for i in brief.sources if i.id in used or not used)
    )
    return CardDeck(
        template=template,  # type: ignore[arg-type]
        brand=DeckBrand(
            name=brand.name,
            primary_color=brand.primary_color,
            accent_color=brand.accent_color,
            logo_path=str(brand.logo_path) if brand.logo_path else None,
        ),
        persona=DeckPersona(name=persona.name, handle=persona.handle),
        labels=DeckLabels(virtual_person=True, ad=brief.is_ad, ai_generated_image=False),
        slides=[
            CoverSlide(title=out["cover_title"].strip(), subtitle=out["cover_subtitle"].strip()),
            *middle,
            OutroSlide(cta=out["cta"].strip() or "저장하고 다시 보기", sources=sources[:6]),
        ],
    )


class ClaudeTextGenerator:
    def __init__(
        self,
        client: Any,
        model: str,
        cost_sink: CostSink | None = None,
        use_fallbacks: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.cost_sink = cost_sink
        self.use_fallbacks = use_fallbacks

    def _call(self, ref: str, **kwargs: Any) -> str:
        if self.use_fallbacks:
            response = self.client.beta.messages.create(
                model=self.model, betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        else:
            response = self.client.messages.create(model=self.model, **kwargs)
        usage = getattr(response, "usage", None)
        if self.cost_sink and usage is not None:
            model = getattr(response, "model", None) or self.model
            self.cost_sink.record_cost(
                "llm", "anthropic", claude_cost(model, usage.input_tokens, usage.output_tokens), ref
            )
        if response.stop_reason == "refusal":
            raise AdapterError("생성 요청이 거절됐습니다(refusal)")
        if response.stop_reason == "max_tokens":
            raise AdapterError("생성 결과가 max_tokens에서 잘렸습니다")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise AdapterError(f"생성 응답에 텍스트가 없습니다(stop_reason={response.stop_reason})")
        return text

    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent:
        raw = self._call(
            f"generate:{brief.template}",
            max_tokens=16000,
            system=_system(persona, brand),
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _LLM_SCHEMA},
            },
            messages=[{"role": "user", "content": _user(brief)}],
        )
        out = json.loads(raw)
        deck = build_deck(brief.template, out, brand, persona, brief)
        return GeneratedContent(
            topic=out["topic"].strip() or brief.topic,
            template=brief.template,
            deck=deck,
            instagram_caption=out["instagram_caption"].strip(),
            threads_text=out["threads_text"].strip(),
            blog_title=out["blog_title"].strip(),
            blog_markdown=out["blog_markdown"].strip(),
            claims=[
                Claim(text=c["text"], source_item_id=c["source_item_id"]) for c in out["claims"]
            ],
        )

    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str:
        schema = {
            "type": "object",
            "properties": {"reply": _STR},
            "required": ["reply"],
            "additionalProperties": False,
        }
        raw = self._call(
            "reply",
            max_tokens=2000,
            system=(
                f"당신은 AI 가상 인플루언서 '{persona.name}'입니다. 말투: {persona.tone}. "
                f"인물상: {persona.character}. 내 게시물에 달린 댓글에 120자 이내로 답글을 씁니다. "
                "직접 써 본 것처럼 말하지 않고, 모르는 사실은 지어내지 않으며, 가격·환불·법적 질문에는 답하지 않습니다. "
                f"금칙어: {', '.join(persona.banned_words) or '-'}."
            ),
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[
                {
                    "role": "user",
                    "content": f"게시물 요약:\n{post_context[:1500]}\n\n"
                    f"댓글 작성자: {comment.author_name}\n댓글: {comment.text}",
                }
            ],
        )
        return json.loads(raw)["reply"].strip()
