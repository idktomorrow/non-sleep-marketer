"""Claude 텍스트 생성기: 카드뉴스 원고·캡션·스레드 글·블로그 글·대댓글.

프롬프트·스키마·덱 조립은 공급자 무관 모듈(prompts.py)에 있고, 여기는 Claude SDK 호출과 응답 해석만 한다.
- Sonnet 5.5는 안전 분류기가 요청을 거절할 수 있어 server-side fallback(`fallbacks: "default"`)을 켠다.
- 거절·잘림·JSON 파싱 실패는 모두 AdapterError(이 자료는 건너뜀). 다른 예외는 "나중에 재시도".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nsm.adapters.generator.prompts import (
    LLM_SCHEMA,
    MAX_MIDDLE_SLIDES,
    MEME_FORMAT_SCHEMA,
    MEME_IDEAS_SCHEMA,
    MEME_SCRIPT_SCHEMA,
    REPLY_CONTEXT_CHARS,
    REPLY_SCHEMA,
    REPLY_SYSTEM,
    TEMPLATE_GUIDE,
    build_deck,
    content_from_output,
    format_plan_from_output,
    ideas_from_output,
    llm_schema,
    meme_format_system,
    meme_format_user,
    meme_ideas_user,
    meme_script_user,
    meme_system,
    reply_system,
    reply_user,
    script_from_output,
    system_prompt,
    user_prompt,
)
from nsm.adapters.pricing import claude_cost
from nsm.domain.models import Brand, Comment, ContentBrief, GeneratedContent, Persona
from nsm.ports import (
    AdapterError,
    CostSink,
    MemeFormatPlan,
    MemeFormatRequest,
    MemeIdea,
    MemeRequest,
    MemeScript,
)

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 예전 이름(테스트·다른 모듈 호환)
_LLM_SCHEMA = LLM_SCHEMA
_TEMPLATE_GUIDE = TEMPLATE_GUIDE
_system = system_prompt
_user = user_prompt
__all__ = [
    "FALLBACK_BETA",
    "MAX_MIDDLE_SLIDES",
    "REPLY_CONTEXT_CHARS",
    "REPLY_SYSTEM",
    "ClaudeTextGenerator",
    "build_deck",
    "llm_schema",
]


class ClaudeTextGenerator:
    def __init__(
        self,
        client: Any,
        model: str,
        cost_sink: CostSink | None = None,
        use_fallbacks: bool = True,
        include_blog: bool = True,
        tz: str = "Asia/Seoul",
    ) -> None:
        self.client = client
        self.model = model
        self.cost_sink = cost_sink
        self.use_fallbacks = use_fallbacks
        self.include_blog = include_blog
        self.tz = tz

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

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        # 깨진 JSON은 이 자료에 한한 실패로 본다(예전엔 JSONDecodeError가 새어 나가 "나중에 재시도"가 됐다, S13)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AdapterError(f"생성 응답 JSON 파싱 실패: {e}") from e

    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent:
        raw = self._call(
            f"generate:{brief.template}",
            max_tokens=16000,
            system=system_prompt(persona, brand, self.include_blog),
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": llm_schema(self.include_blog, brief.template),
                },
            },
            messages=[
                {
                    "role": "user",
                    "content": user_prompt(brief, datetime.now(ZoneInfo(self.tz)).date()),
                }
            ],
        )
        return content_from_output(self._parse(raw), brief, brand, persona)

    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str:
        raw = self._call(
            "reply",
            max_tokens=2000,
            system=reply_system(persona),
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": REPLY_SCHEMA},
            },
            messages=[{"role": "user", "content": reply_user(comment, post_context)}],
        )
        return str(self._parse(raw)["reply"]).strip()

    # ------------------------------------------------------------------ ScenarioWriter(S14)
    def _meme_call(
        self, ref: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        raw = self._call(
            ref,
            max_tokens=4000,
            system=system,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        return self._parse(raw)

    def propose_meme_ideas(self, req: MemeRequest) -> list[MemeIdea]:
        out = self._meme_call(
            "meme:ideas", meme_system(req), meme_ideas_user(req), MEME_IDEAS_SCHEMA
        )
        ideas = ideas_from_output(out)
        if not ideas:
            raise AdapterError("밈 아이디어가 비었습니다")
        return ideas

    def write_meme_script(self, req: MemeRequest, idea: MemeIdea) -> MemeScript:
        out = self._meme_call(
            "meme:script", meme_system(req), meme_script_user(req, idea), MEME_SCRIPT_SCHEMA
        )
        try:
            return script_from_output(out, idea)
        except (KeyError, TypeError) as e:
            raise AdapterError(f"밈 대본 형식이 맞지 않습니다: {e}") from e

    def plan_meme_format(self, req: MemeFormatRequest) -> MemeFormatPlan:
        """유명·유행 밈 형식 하나를 골라 구도·연출·레이아웃까지 재해석(S17 제안, ADR 0061)."""
        out = self._meme_call(
            "meme:format", meme_format_system(req), meme_format_user(req), MEME_FORMAT_SCHEMA
        )
        try:
            return format_plan_from_output(out, req)
        except (KeyError, TypeError, ValueError) as e:
            raise AdapterError(f"밈 형식 계획이 맞지 않습니다: {e}") from e
