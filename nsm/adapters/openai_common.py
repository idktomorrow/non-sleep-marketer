"""OpenAI REST 공용 클라이언트(S13 제안): httpx로 직접 부르고(SDK 의존 없음) 오류를 **분류만** 한다.

재시도는 여기서 하지 않는다(피드백 5). 텍스트·이미지, 전송 전·후에 따라 정책이 다르므로 호출자가 정한다:
- phase=before_send(연결 실패·DNS): 요청이 서버에 닿지 않았다 → 재예약해도 안전
- phase=after_send(응답 없음·읽기 타임아웃): 생성·과금 여부가 불명확 → 이미지는 unknown, 텍스트는 1회만
- kind=quota/auth: 다시 보내도 같다 → 영구 실패 + 알림. rate_limit/overloaded: Retry-After 뒤 재예약
- kind=moderation: 정책 차단(이미지) → 사유를 사람에게
키는 헤더로만 보내고 오류 문구·로그에 남기지 않는다. 응답 헤더 x-request-id는 추적용으로 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.openai.com/v1"
Kind = Literal[
    "auth",
    "quota",
    "rate_limit",
    "overloaded",
    "moderation",
    "invalid",
    "timeout_unknown",
    "network",
]
Phase = Literal["before_send", "after_send"]

_QUOTA_CODES = ("insufficient_quota", "credit_balance_exhausted")
_QUOTA_SUFFIXES = ("_spend_limit_exceeded", "_usage_limit_exceeded")


@dataclass
class OpenAIError(Exception):
    kind: Kind
    phase: Phase
    message: str
    status: int = 0
    code: str = ""
    request_id: str = ""
    retry_after: float | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        where = f"HTTP {self.status} " if self.status else ""
        rid = f" [{self.request_id}]" if self.request_id else ""
        return f"OpenAI {self.kind}: {where}{self.message}{rid}"

    @property
    def permanent(self) -> bool:
        """다시 보내도 같은 결과(키·크레딧·정책·요청 형식)."""
        return self.kind in ("auth", "quota", "moderation", "invalid")

    @property
    def reschedulable(self) -> bool:
        """요청이 닿지 않았거나 잠깐의 한도라 나중에 다시 보내도 되는 실패."""
        return self.kind in ("rate_limit", "overloaded", "network")


def classify_response(r: httpx.Response) -> OpenAIError:
    """2xx가 아닌 응답을 분류한다."""
    rid = r.headers.get("x-request-id", "")
    try:
        err = (r.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    code = str(err.get("code") or "")
    message = " ".join(str(err.get("message") or r.text[:200]).split())
    retry_after = None
    if r.headers.get("retry-after"):
        try:
            retry_after = float(r.headers["retry-after"])
        except ValueError:
            retry_after = None
    status = r.status_code
    kind: Kind
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        is_quota = (
            code in _QUOTA_CODES
            or code.endswith(_QUOTA_SUFFIXES)
            or err.get("type") == "insufficient_quota"
        )
        kind = "quota" if is_quota else "rate_limit"
    elif status >= 500:
        kind = "overloaded"
    elif code == "moderation_blocked":
        kind = "moderation"
    else:
        kind = "invalid"
    details = err.get("moderation_details") if kind == "moderation" else None
    return OpenAIError(
        kind=kind,
        phase="after_send",
        message=message,
        status=status,
        code=code,
        request_id=rid,
        retry_after=retry_after,
        details=details,
    )


def classify_transport(e: httpx.HTTPError) -> OpenAIError:
    """연결 단계 예외를 분류한다. 요청이 서버에 닿았을 수 있으면 after_send(불명확)."""
    if isinstance(e, httpx.ConnectError | httpx.ConnectTimeout | httpx.ProxyError):
        return OpenAIError("network", "before_send", f"연결 실패 {type(e).__name__}")
    # ReadTimeout·WriteTimeout·RemoteProtocolError·ReadError: 보냈는데 답을 못 받았다
    return OpenAIError("timeout_unknown", "after_send", f"응답 없음 {type(e).__name__}")


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
        timeout: float = 180.0,
    ) -> None:
        self._key = api_key
        self.client = client or httpx.Client(timeout=timeout)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    def post_json(self, path: str, body: dict[str, Any], timeout: float | None = None) -> dict:
        return self._send("POST", path, json=body, timeout=timeout)

    def post_multipart(
        self,
        path: str,
        data: dict[str, Any],
        files: list[tuple[str, tuple[str, bytes, str]]],
        timeout: float | None = None,
    ) -> dict:
        return self._send("POST", path, data=data, files=files, timeout=timeout)

    def _send(self, method: str, path: str, timeout: float | None = None, **kwargs: Any) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            r = self.client.request(
                method, url, headers=self._headers(), timeout=timeout or self.timeout, **kwargs
            )
        except httpx.HTTPError as e:
            raise classify_transport(e) from None  # 키가 든 요청 객체를 예외 체인에 남기지 않는다
        if r.status_code >= 400:
            err = classify_response(r)
            log.warning("OpenAI %s %s 실패: %s", method, path, err)
            raise err
        try:
            out = r.json()
        except ValueError as e:
            raise OpenAIError(
                "invalid",
                "after_send",
                f"JSON이 아닌 응답: {e}",
                r.status_code,
                request_id=r.headers.get("x-request-id", ""),
            ) from None
        if isinstance(out, dict):
            out.setdefault("_request_id", r.headers.get("x-request-id", ""))
        return out
