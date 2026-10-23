"""OpenAI 텍스트 생성기(S13 제안): Responses API + strict JSON 스키마로 카드뉴스 원고·답글을 만든다.

- `POST /v1/responses`, `text.format={type: json_schema, strict: true}`, `store: false`(저장 안 함),
  `reasoning.effort`는 설정(기본 low). 프롬프트·스키마·덱 조립은 Claude와 같은 prompts.py.
- 잘림(`status=incomplete`)·거절(`refusal`)·JSON/스키마 불일치는 AdapterError(이 자료는 건너뜀).
- 키·크레딧 오류(auth/quota)는 ProviderUnavailable(영구, 알림 대상). 한도(rate_limit)·과부하·연결 실패는
  AdapterError가 아니라 TransientError로 올려 호출자가 재예약한다. 전송 뒤 응답 없음은 1회만 다시 시도.
- 비용은 원장이 있으면 예약→확정(usage 토큰 × 모델 단가), 없으면 record_cost.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from nsm.adapters.generator.prompts import (
    MEME_FORMAT_SCHEMA,
    MEME_IDEAS_SCHEMA,
    MEME_SCRIPT_SCHEMA,
    REPLY_SCHEMA,
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
from nsm.adapters.openai_common import OpenAIClient, OpenAIError
from nsm.adapters.pricing import openai_text_cost
from nsm.domain.models import Brand, Comment, ContentBrief, GeneratedContent, Persona
from nsm.ports import (
    AdapterError,
    CostLedger,
    CostSink,
    MemeFormatPlan,
    MemeFormatRequest,
    MemeIdea,
    MemeRequest,
    MemeScript,
)

log = logging.getLogger(__name__)

GENERATE_MAX_TOKENS = 16000  # 추론 토큰 포함 상한(공식 문서: 추론 모델은 넉넉히)
REPLY_MAX_TOKENS = 3000
ESTIMATE_GENERATE_USD = 0.06  # 예약액(확정 전). gpt-6.1-sol 기준 입력 6k·출력 4k 정도
ESTIMATE_REPLY_USD = 0.01
ESTIMATE_MEME_USD = 0.02
MEME_MAX_TOKENS = 4000


class ProviderUnavailable(Exception):
    """키·크레딧·권한 문제: 다시 보내도 같다. 사람이 고쳐야 한다(알림)."""


class TransientError(Exception):
    """잠깐의 한도·과부하·연결 실패: 나중에 다시 보내면 된다(재예약)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OpenAITextGenerator:
    provider = "openai"
    cost_kind = "llm"  # 비용 원장 종류. 분류기로 쓸 때는 "classifier"(S16 리뷰)

    def __init__(
        self,
        client: OpenAIClient,
        model: str,
        cost_sink: CostSink | None = None,
        effort: str = "low",
        include_blog: bool = True,
        tz: str = "Asia/Seoul",
        safety_identifier: str = "",
    ) -> None:
        self.client = client
        self.model = model
        self.cost_sink = cost_sink
        self.effort = effort
        self.include_blog = include_blog
        self.tz = tz
        self.safety_identifier = safety_identifier

    # ------------------------------------------------------------------ 호출
    def _body(
        self, system: str, user: str, schema: dict[str, Any], name: str, max_tokens: int
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": user}]}],
            "max_output_tokens": max_tokens,
            "store": False,
            "text": {
                "format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}
            },
        }
        if self.effort:
            body["reasoning"] = {"effort": self.effort}
        if self.safety_identifier:
            body["safety_identifier"] = self.safety_identifier
        return body

    def _call(self, ref: str, body: dict[str, Any], estimate: float) -> dict[str, Any]:
        ledger = self.cost_sink if isinstance(self.cost_sink, CostLedger) else None
        rid = ledger.reserve(self.cost_kind, "openai", estimate, ref, self.model) if ledger else ""
        try:
            out = self._post_once(body)
        except OpenAIError as e:
            if not (e.phase == "after_send" and e.kind == "timeout_unknown"):
                raise self._fail(ledger, rid, e) from None
            # 텍스트는 글만 다시 받으면 되니 1회만 다시 보낸다. 첫 요청 비용은 미확인으로 남긴다
            if ledger:
                ledger.mark_unknown(rid, e.request_id)
                rid = ledger.reserve(self.cost_kind, "openai", estimate, ref + ":retry", self.model)
            try:
                out = self._post_once(body)
            except OpenAIError as e2:
                raise self._fail(ledger, rid, e2) from None
        usage = out.get("usage") or {}
        usd = openai_text_cost(
            str(out.get("model") or self.model),
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
        )
        if ledger:
            ledger.confirm(
                rid,
                usd,
                "confirmed" if usage else "estimated",
                detail={"model": out.get("model") or self.model, "usage": usage},
                request_id=str(out.get("_request_id") or ""),
            )
        elif self.cost_sink:
            self.cost_sink.record_cost(self.cost_kind, "openai", usd, ref)
        return out

    def _post_once(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.client.post_json("responses", body)

    @staticmethod
    def _fail(ledger: CostLedger | None, rid: str, e: OpenAIError) -> Exception:
        """원장 정리 뒤 호출자가 올릴 예외를 돌려준다."""
        if ledger and rid:
            if e.phase == "after_send" and e.kind == "timeout_unknown":
                ledger.mark_unknown(rid, e.request_id)
            else:
                ledger.release(rid)
        if e.kind in ("auth", "quota"):
            return ProviderUnavailable(str(e))
        if e.reschedulable or e.kind == "timeout_unknown":
            return TransientError(str(e), e.retry_after)
        return AdapterError(str(e))

    # ------------------------------------------------------------------ 응답 해석
    @staticmethod
    def _text(out: dict[str, Any]) -> str:
        if out.get("status") == "incomplete":
            reason = (out.get("incomplete_details") or {}).get("reason", "")
            raise AdapterError(f"생성 결과가 잘렸습니다(incomplete: {reason or 'unknown'})")
        if out.get("status") not in (None, "completed"):
            raise AdapterError(f"생성 응답 상태가 {out.get('status')}입니다")
        for item in out.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    raise AdapterError(
                        f"생성 요청이 거절됐습니다(refusal): {part.get('refusal', '')}"
                    )
                if part.get("type") == "output_text":
                    return str(part.get("text") or "")
        raise AdapterError("생성 응답에 텍스트가 없습니다")

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise AdapterError(f"생성 응답 JSON 파싱 실패: {e}") from e

    # ------------------------------------------------------------------ TextGenerator
    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent:
        body = self._body(
            system_prompt(persona, brand, self.include_blog),
            user_prompt(brief, datetime.now(ZoneInfo(self.tz)).date()),
            llm_schema(self.include_blog, brief.template),
            "card_deck",
            GENERATE_MAX_TOKENS,
        )
        out = self._call(f"generate:{brief.template}", body, ESTIMATE_GENERATE_USD)
        data = self._parse(self._text(out))
        try:
            return content_from_output(data, brief, brand, persona)
        except (ValidationError, KeyError, TypeError) as e:
            raise AdapterError(f"생성 결과가 덱 계약에 맞지 않습니다: {str(e)[:200]}") from e

    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str:
        body = self._body(
            reply_system(persona),
            reply_user(comment, post_context),
            REPLY_SCHEMA,
            "reply",
            REPLY_MAX_TOKENS,
        )
        out = self._call("reply", body, ESTIMATE_REPLY_USD)
        return str(self._parse(self._text(out))["reply"]).strip()

    # ------------------------------------------------------------------ ScenarioWriter(S14)
    def propose_meme_ideas(self, req: MemeRequest) -> list[MemeIdea]:
        body = self._body(
            meme_system(req), meme_ideas_user(req), MEME_IDEAS_SCHEMA, "meme_ideas", MEME_MAX_TOKENS
        )
        out = self._call("meme:ideas", body, ESTIMATE_MEME_USD)
        ideas = ideas_from_output(self._parse(self._text(out)))
        if not ideas:
            raise AdapterError("밈 아이디어가 비었습니다")
        return ideas

    def write_meme_script(self, req: MemeRequest, idea: MemeIdea) -> MemeScript:
        body = self._body(
            meme_system(req),
            meme_script_user(req, idea),
            MEME_SCRIPT_SCHEMA,
            "meme_script",
            MEME_MAX_TOKENS,
        )
        out = self._call("meme:script", body, ESTIMATE_MEME_USD)
        try:
            return script_from_output(self._parse(self._text(out)), idea)
        except (KeyError, TypeError) as e:
            raise AdapterError(f"밈 대본 형식이 맞지 않습니다: {e}") from e

    def place_photos(self, cover_text: str, slides, photos, items=()) -> dict:
        """실제 사진과 카드 장 짝짓기(S18, 포트 PhotoPlacer): 사진을 작게 줄여 보여 주고 내용이 맞는 장만 고르게 한다."""
        from nsm.adapters.generator import photo_place as pp

        body = self._body(pp.SYSTEM, "", pp.SCHEMA, "photo_placement", pp.MAX_TOKENS)
        body["input"] = [
            {"role": "user", "content": pp.user_content(cover_text, slides, photos, items)}
        ]
        return self._parse(self._text(self._call("photo:match", body, pp.ESTIMATE_USD)))

    def observe_photos(self, photos) -> list[dict]:
        """원고를 쓰기 전 실제 사진 관찰(S18 편집 밀도, 포트 PhotoObserver): 보이는 것·디테일·위치."""
        from nsm.adapters.generator import photo_place as pp

        body = self._body(pp.OBSERVE_SYSTEM, "", pp.OBSERVE_SCHEMA, "photo_observe", pp.MAX_TOKENS)
        body["input"] = [{"role": "user", "content": pp.observe_content(photos)}]
        out = self._parse(self._text(self._call("photo:observe", body, pp.OBSERVE_USD)))
        return list(out.get("photos", []))

    def review_meme_image(self, image_path: str, brief: str) -> dict:
        """완성된 밈 그림 검토(S18, 포트 MemeImageReviewer): 재미의 핵심이 그림에 보이는지, 문제는 어느 단계인지."""
        from nsm.adapters.generator import photo_place as pp

        body = self._body(pp.MEME_REVIEW_SYSTEM, "", pp.MEME_REVIEW_SCHEMA, "meme_review", 800)
        body["input"] = [{"role": "user", "content": pp.meme_review_content(image_path, brief)}]
        return self._parse(self._text(self._call("meme:review", body, pp.MEME_REVIEW_USD)))

    def plan_meme_format(self, req: MemeFormatRequest) -> MemeFormatPlan:
        """유명·유행 밈 형식 하나를 골라 구도·연출·레이아웃까지 재해석(S17 제안, ADR 0061)."""
        body = self._body(
            meme_format_system(req),
            meme_format_user(req),
            MEME_FORMAT_SCHEMA,
            "meme_format",
            MEME_MAX_TOKENS * 2,
        )
        out = self._call("meme:format", body, ESTIMATE_MEME_USD * 2)
        try:
            return format_plan_from_output(self._parse(self._text(out)), req)
        except (KeyError, TypeError, ValueError) as e:
            raise AdapterError(f"밈 형식 계획이 맞지 않습니다: {e}") from e
