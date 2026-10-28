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

from nsm.config.secrets import KEY_NAMES, KEY_USES, KEYS, SecretStore

log = logging.getLogger(__name__)

KEY_LABELS = KEY_NAMES  # 설정 화면에 보이는 이름(마법사와 같은 원본, nsm/config/secrets.py)
MIN_KEY_LEN = 20
CHECK_TIMEOUT_S = 10.0

# 키 확인 결과(코드 → 화면 문구). 응답 본문은 싣지 않는다(키·계정 정보가 섞일 수 있음)
CHECK_TEXT = {
    "ok": "연결됨",
    "invalid": "키가 틀렸거나 만료됨",
    "limit": "키는 맞지만 한도·결제 확인 필요",
    "network": "연결 실패(네트워크·시간 초과)",
    "missing": "등록된 키가 없음",
    "unknown": "확인하지 못함",  # 404 등 예상 밖 응답: 키가 맞다고도 틀리다고도 말하지 않는다
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
    if (
        code in (402, 429) or code == 400
    ):  # 한도(429)·결제(402)·요금제·지역(Gemini 400 FAILED_PRECONDITION)
        return "limit", code
    return "unknown", code  # 404 등: 주소가 바뀌었을 수 있다. 키가 맞다고 말하지 않는다


def check_text(result: str, status: int | None) -> str:
    text = CHECK_TEXT.get(result, "알 수 없음")
    return (
        f"{text}(HTTP {status})" if status and result in ("limit", "network", "unknown") else text
    )


def key_status(secrets: SecretStore | None) -> list[dict[str, Any]]:
    """화면용: 키마다 이름·라벨·쓰임·등록 여부·환경 변수 여부만. 값은 담지 않는다."""
    if secrets is None:
        return []
    out = []
    for k in KEYS:
        try:
            present = bool(secrets.get(k))
        except Exception:  # noqa: BLE001  keyring 백엔드 없음 등
            present = False
        out.append(
            {
                "name": k,
                "label": KEY_LABELS.get(k, k),
                "use": KEY_USES.get(k, ""),
                "present": present,
                "env": secrets.is_env(k),
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

    def _verify(name: str, value: str | None) -> tuple[str, int | None]:
        return (state.check_key or check_key)(name, value)

    def _check(name: str, found: tuple[str, int | None] | None = None) -> dict[str, str]:
        result, status = found or _verify(name, state.secrets.get(name))
        return {
            "key_check": name,
            "key_check_text": check_text(result, status),
            "key_check_code": result,
        }

    @app.post("/settings/keys/{name}", dependencies=[Depends(check_csrf)])
    def register_key(name: str, value: str = Form("")) -> RedirectResponse:
        _known(name)
        value = value.strip()
        label = KEY_LABELS.get(name, name)
        if len(value) < MIN_KEY_LEN or any(ch.isspace() for ch in value):
            return _redirect(key_error="키 형식이 아닙니다(공백 없이 20자 이상)")
        if state.secrets.is_env(name):
            # 환경 변수가 keyring보다 먼저 읽힌다: 여기 저장해도 쓰이지 않으니 저장하지 않는다(리뷰)
            return _redirect(
                key_error=f"{label} 키는 환경 변수 키가 먼저 쓰여 여기 저장해도 쓰이지 않습니다(환경 변수를 지우고 다시 켜세요)"
            )
        # 저장 전에 새 키부터 확인: 틀린 키가 잘 되던 키를 덮어쓰지 않게(리뷰). 연결 실패·한도는 판단 못 하니 저장
        found = _verify(name, value)
        if found[0] == "invalid":
            return _redirect(
                key_error=f"{label} 키가 틀렸거나 만료돼 저장하지 않았습니다(원래 키는 그대로)",
                **_check(name, found),
            )
        try:
            state.secrets.set(name, value)
        except Exception as e:  # noqa: BLE001  keyring 백엔드 오류. 값은 로그에 남기지 않는다
            log.warning("키 저장 실패(%s): %s", name, type(e).__name__)
            return _redirect(key_error=f"키를 저장하지 못했습니다({type(e).__name__})")
        _changed()
        log.info("API 키 저장: %s", name)
        return _redirect(
            key_msg=f"{label} 키를 저장했습니다. 다음 작업부터 씁니다", **_check(name, found)
        )

    @app.post("/settings/keys/{name}/check", dependencies=[Depends(check_csrf)])
    def verify_key(name: str) -> RedirectResponse:
        _known(name)
        return _redirect(**_check(name))

    @app.post("/settings/keys/{name}/delete", dependencies=[Depends(check_csrf)])
    def delete_key(name: str) -> RedirectResponse:
        _known(name)
        label = KEY_LABELS.get(name, name)
        if state.secrets.is_env(name):
            # 환경 변수는 프로그램이 지울 수 없다: 지웠다고 말하지 않는다
            return _redirect(
                key_error=f"{label} 키는 환경 변수 키라 여기서는 못 지웁니다(환경 변수를 지우고 다시 켜세요)"
            )
        state.secrets.delete(name)
        if state.secrets.get(
            name
        ):  # keyring이 잠겼거나 권한 문제로 못 지웠다: 지웠다고 말하지 않는다(리뷰)
            log.warning("API 키 삭제 실패: %s", name)
            return _redirect(
                key_error=f"{label} 키를 지우지 못했습니다(자격 증명 관리자를 확인하세요)"
            )
        _changed()
        log.info("API 키 삭제: %s", name)
        return _redirect(key_msg=f"{label} 키를 지웠습니다")
