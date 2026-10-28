"""설정 화면의 API 키 칸(배치 C35, ADR 0028 개정 1). 출발점은 PR #101(S13)의 keys_routes.

- 키는 keyring에만 저장한다. 값은 응답·로그·설정 파일·DB 어디에도 다시 쓰지 않는다(끝 몇 자리도 안 보여 줌).
- 화면에는 키마다 "등록됨/없음"(과 환경 변수 여부)만 보인다.
- "확인"은 공급자의 모델 목록 조회로 키를 검증한다(생성 없음, 비용 0). 응답 본문은 화면에 싣지 않는다.
- 저장·삭제 뒤 `on_secrets_changed()`로 키를 쓰는 부품을 새로 만들어 재시작 없이 다음 작업부터 쓴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse

from nsm.config.secrets import KEYS, SecretStore

log = logging.getLogger(__name__)

# 우리 구조대로의 순서와 쓰임(Claude 필수 → Gemini 무료 분류 → OpenAI 선택)
KEY_ORDER = ("anthropic_api_key", "gemini_api_key", "openai_api_key")
KEY_LABELS = {
    "anthropic_api_key": "Claude",
    "gemini_api_key": "Gemini",
    "openai_api_key": "OpenAI",
}
KEY_USES = {
    "anthropic_api_key": "원고·답글을 씁니다. 필수",
    "gemini_api_key": "자료·댓글을 분류합니다. 무료 구간(없으면 Claude로 분류)",
    "openai_api_key": "표지 배경 이미지. 선택이며 기본은 꺼짐(설정 image_generation)",
}
MIN_KEY_LEN = 20
CHECK_TIMEOUT_S = 10.0

# 키 확인 결과(코드 → 화면 문구). 응답 본문은 싣지 않는다(키·계정 정보가 섞일 수 있음)
CHECK_TEXT = {
    "ok": "연결됨",
    "invalid": "키가 틀렸거나 만료됨",
    "limit": "키는 맞지만 한도·결제 확인 필요",
    "network": "연결 실패(네트워크·시간 초과)",
    "missing": "등록된 키가 없음",
}


def _request(name: str, key: str) -> tuple[str, dict[str, str]]:
    if name == "anthropic_api_key":
        return "https://api.anthropic.com/v1/models", {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
    if name == "gemini_api_key":
        return "https://generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": key}
    if name == "openai_api_key":
        return "https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"}
    raise ValueError(name)


def check_key(
    name: str, key: str | None, transport: httpx.BaseTransport | None = None
) -> tuple[str, int | None]:
    """모델 목록을 한 번 조회해 (결과 코드, HTTP 상태)를 돌려준다. 생성 호출이 아니라 비용이 없다.

    Gemini는 틀린 키에 401이 아니라 400(`API_KEY_INVALID`)을 돌려준다(2026-10-29 가짜 키로 확인).
    """
    if not key:
        return "missing", None
    url, headers = _request(name, key)
    try:
        with httpx.Client(timeout=CHECK_TIMEOUT_S, transport=transport) as c:
            r = c.get(url, headers=headers)
    except httpx.HTTPError as e:
        log.info("키 확인 연결 실패(%s): %s", name, type(e).__name__)
        return "network", None
    code = r.status_code
    if 200 <= code < 300:
        return "ok", code
    if code in (401, 403) or (code == 400 and "API_KEY_INVALID" in r.text):
        return "invalid", code
    if code >= 500:
        return "network", code
    return "limit", code  # 429(한도), 402·400(결제·요금제) 등


def check_text(result: str, status: int | None) -> str:
    text = CHECK_TEXT.get(result, "알 수 없음")
    return f"{text}(HTTP {status})" if status and result in ("limit", "network") else text


def key_status(secrets: SecretStore | None) -> list[dict[str, Any]]:
    """화면용: 키마다 이름·라벨·쓰임·등록 여부·환경 변수 여부만. 값은 담지 않는다."""
    if secrets is None:
        return []
    out = []
    for k in [*KEY_ORDER, *(k for k in KEYS if k not in KEY_ORDER)]:
        try:
            present = bool(secrets.get(k))
        except Exception:  # noqa: BLE001  keyring 백엔드 없음 등
            present = False
        is_env = getattr(secrets, "is_env", None)
        out.append(
            {
                "name": k,
                "label": KEY_LABELS.get(k, k),
                "use": KEY_USES.get(k, ""),
                "present": present,
                "env": bool(is_env and is_env(k)),
            }
        )
    return out


def install_keys_routes(app: FastAPI, state: Any, check_csrf: Callable[..., Any]) -> None:
    def _redirect(**q: str) -> RedirectResponse:
        return RedirectResponse(f"/settings?{urlencode(q)}#api-keys", status_code=303)

    def _known(name: str) -> None:
        if state.secrets is None:
            raise HTTPException(status_code=404)
        if name not in KEYS:
            raise HTTPException(status_code=400, detail="알 수 없는 키 이름")

    def _changed() -> None:
        if state.on_secrets_changed:
            state.on_secrets_changed()

    def _check(name: str) -> dict[str, str]:
        checker = state.check_key or check_key
        result, status = checker(name, state.secrets.get(name))
        return {
            "key_check": name,
            "key_check_text": check_text(result, status),
            "key_check_code": result,
        }

    @app.post("/settings/keys/{name}", dependencies=[Depends(check_csrf)])
    def register_key(name: str, value: str = Form("")) -> RedirectResponse:
        _known(name)
        value = value.strip()
        if len(value) < MIN_KEY_LEN or any(ch.isspace() for ch in value):
            return _redirect(key_error="키 형식이 아닙니다(공백 없이 20자 이상)")
        try:
            state.secrets.set(name, value)
        except Exception as e:  # noqa: BLE001  keyring 백엔드 오류. 값은 로그에 남기지 않는다
            log.warning("키 저장 실패(%s): %s", name, type(e).__name__)
            return _redirect(key_error=f"키를 저장하지 못했습니다({type(e).__name__})")
        del value
        _changed()
        log.info("API 키 저장: %s", name)
        label = KEY_LABELS.get(name, name)
        return _redirect(key_msg=f"{label} 키를 저장했습니다. 다음 작업부터 씁니다", **_check(name))

    @app.post("/settings/keys/{name}/check", dependencies=[Depends(check_csrf)])
    def verify_key(name: str) -> RedirectResponse:
        _known(name)
        return _redirect(**_check(name))

    @app.post("/settings/keys/{name}/delete", dependencies=[Depends(check_csrf)])
    def delete_key(name: str) -> RedirectResponse:
        _known(name)
        label = KEY_LABELS.get(name, name)
        is_env = getattr(state.secrets, "is_env", None)
        if is_env and is_env(name):
            # 환경 변수는 프로그램이 지울 수 없다: 지웠다고 말하지 않는다
            return _redirect(
                key_error=f"{label} 키는 환경 변수 키라 여기서는 못 지웁니다(환경 변수를 지우고 다시 켜세요)"
            )
        state.secrets.delete(name)
        _changed()
        log.info("API 키 삭제: %s", name)
        return _redirect(key_msg=f"{label} 키를 지웠습니다")
