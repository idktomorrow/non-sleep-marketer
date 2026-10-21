"""API 키 화면(S13 제안): 등록 여부만 보여 주고, 등록·삭제는 keyring에만 쓴다.

- 키 값은 응답·로그·설정 파일 어디에도 다시 쓰지 않는다(마스킹한 값도 보여 주지 않는다).
- 등록 직후 `on_secrets_changed()`로 어댑터 묶음을 새로 만들어 재시작 없이 다음 작업부터 쓴다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse

from nsm.config.secrets import KEYS, SecretStore

log = logging.getLogger(__name__)

KEY_LABELS = {
    "openai_api_key": "OpenAI API 키 (글·답글·그림)",
    "anthropic_api_key": "Claude API 키 (글·답글, 선택)",
    "gemini_api_key": "Gemini API 키 (자료 분류, 무료 구간)",
}
MIN_KEY_LEN = 20


def key_status(secrets: SecretStore | None) -> list[dict[str, Any]]:
    """화면용: 키마다 이름·라벨·등록 여부·출처(환경 변수/keyring)만."""
    if secrets is None:
        return []
    out = []
    order = ("openai_api_key", "anthropic_api_key", "gemini_api_key")  # OpenAI 하나면 되니 맨 위
    for k in [*order, *(k for k in KEYS if k not in order)]:
        try:
            present = bool(secrets.get(k))
        except Exception:  # noqa: BLE001  keyring 백엔드 없음 등
            present = False
        out.append({"name": k, "label": KEY_LABELS.get(k, k), "present": present})
    return out


def install_keys_routes(
    app: FastAPI,
    state: Any,
    check_csrf: Callable[..., Any],
) -> None:
    def _redirect(msg: str = "", error: str = "") -> RedirectResponse:
        from urllib.parse import urlencode

        q = urlencode({"key_msg": msg} if msg else {"key_error": error})
        return RedirectResponse(f"/settings?{q}#ai-keys", status_code=303)

    @app.post("/settings/keys/{name}", dependencies=[Depends(check_csrf)])
    def register_key(name: str, value: str = Form("")) -> RedirectResponse:
        if state.secrets is None:
            raise HTTPException(status_code=404)
        if name not in KEYS:
            raise HTTPException(status_code=400, detail="알 수 없는 키 이름")
        value = value.strip()
        if len(value) < MIN_KEY_LEN or any(ch.isspace() for ch in value):
            return _redirect(error="키 형식이 아닙니다(공백 없이 20자 이상)")
        try:
            state.secrets.set(name, value)
        except Exception as e:  # noqa: BLE001  keyring 백엔드 오류
            log.warning("키 저장 실패(%s): %s", name, type(e).__name__)
            return _redirect(error=f"키를 저장하지 못했습니다({type(e).__name__})")
        del value  # 더는 들고 있지 않는다
        if state.on_secrets_changed:
            state.on_secrets_changed()
        log.info("API 키 등록: %s", name)
        return _redirect(
            msg=f"{KEY_LABELS.get(name, name)}를 등록했습니다. 다음 작업부터 바로 씁니다"
        )

    @app.post("/settings/keys/{name}/delete", dependencies=[Depends(check_csrf)])
    def delete_key(name: str) -> RedirectResponse:
        if state.secrets is None:
            raise HTTPException(status_code=404)
        if name not in KEYS:
            raise HTTPException(status_code=400, detail="알 수 없는 키 이름")
        state.secrets.delete(name)
        if state.secrets.get(name):
            # keyring에서는 지웠지만 환경 변수로 들어온 키가 남아 있다: 지웠다고 말하지 않는다(S13 리뷰)
            return _redirect(
                error="환경 변수로 넣은 키는 여기서 지울 수 없습니다(환경 변수를 지우고 다시 켜세요)"
            )
        if state.on_secrets_changed:
            state.on_secrets_changed()
        log.info("API 키 삭제: %s", name)
        return _redirect(msg=f"{KEY_LABELS.get(name, name)}를 지웠습니다")
