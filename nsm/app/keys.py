"""설정 화면의 API 키 칸(배치 C35, ADR 0028 개정 1). 출발점은 PR #101(S13)의 keys_routes.

- 키는 keyring에만 저장한다. 값은 응답·로그·설정 파일·DB 어디에도 다시 쓰지 않는다(끝 몇 자리도 안 보여 줌).
- 화면에는 키마다 "등록됨/없음"(과 환경 변수 여부)만 보인다.
- "확인"은 공급자의 모델 목록 조회로 키를 검증한다(생성 없음, 비용 0). 응답 본문은 화면에 싣지 않는다.
- 저장·삭제 뒤 `on_secrets_changed()`로 키를 쓰는 부품을 새로 만들어 재시작 없이 다음 작업부터 쓴다.
- 만료일 메모(C53, ADR 0077): 키 칸 옆 날짜 칸. 키 칸을 비우고 저장하면 날짜만 바꾸고, 둘 다 비우면 메모를 지운다.
  새 키를 저장하면 옛 날짜는 지우고 이번 날짜만 남긴다. 키를 지우면 날짜·알림·자동 확인 상태도 지운다.
  저장·"확인" 결과는 `key_check`에 바로 적어 첫 화면의 옛 오류 배너가 바로 사라지게 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import RedirectResponse

from nsm.config.secrets import CHECK_TEXT, KEY_NAMES, KEY_USES, KEYS, SecretStore
from nsm.engine import key_watch

log = logging.getLogger(__name__)

KEY_LABELS = KEY_NAMES  # 설정 화면에 보이는 이름(마법사와 같은 원본, nsm/config/secrets.py)
MIN_KEY_LEN = 20
CHECK_TIMEOUT_S = 10.0


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

    def _record(name: str, result: str) -> None:
        """확인 결과를 자동 확인 상태에 반영(C53). 판단 못 한 결과(연결 실패 등)는 옛 결과만 지운다."""
        if result in ("ok", "invalid", "limit"):
            key_watch.record_check(state.repo, name, result, state.clock.now())
        else:
            key_watch.clear_check(state.repo, name)

    def _check(name: str, found: tuple[str, int | None] | None = None) -> dict[str, str]:
        result, status = found or _verify(name, state.secrets.get(name))
        return {
            "key_check": name,
            "key_check_text": check_text(result, status),
            "key_check_code": result,
        }

    @app.post("/settings/keys/{name}", dependencies=[Depends(check_csrf)])
    def register_key(
        name: str, value: str = Form(""), expires: str = Form(""), with_date: str = Form("")
    ) -> RedirectResponse:
        # with_date: 날짜 칸이 있는 폼(C53). 빈 칸은 FastAPI가 기본값으로 읽어 "날짜 칸을 비움"과 구별하려고 둔다
        _known(name)
        value = value.strip()
        label = KEY_LABELS.get(name, name)
        new_date = None
        if expires.strip():
            new_date = key_watch.parse_date(expires)
            if new_date is None:  # 형식이 틀리면 키도 날짜도 저장하지 않는다
                return _redirect(key_error="만료일 형식이 아닙니다(예: 2026-12-12)")
        if not value and with_date:  # 키 칸을 비우고 저장: 만료일 메모만 바꾼다
            if new_date is not None and not state.secrets.get(name):
                return _redirect(
                    key_error=f"{label} 키가 없어 만료일만 적을 수 없습니다(키와 함께 저장하세요)"
                )
            key_watch.set_expiry(state.repo, name, new_date)
            return _redirect(
                key_msg=f"{label} 키 만료일을 {new_date:%m-%d}로 적었습니다"
                if new_date
                else f"{label} 키 만료일 메모를 지웠습니다"
            )
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
        # 새 키: 옛 키의 날짜·알림 단계는 버리고 이번에 적은 날짜만(엉뚱한 알림 방지). 확인 결과도 바로 반영
        old_date = key_watch.expiry_dates(state.repo).get(name)
        key_watch.forget_key(state.repo, name)
        # 날짜 칸에는 옛 날짜가 미리 채워져 있다: 그대로 보냈으면 옛 키의 날짜라 버린다(리뷰, 새 만료일만 남김)
        if new_date is not None and new_date != old_date:
            key_watch.set_expiry(state.repo, name, new_date)
        _record(name, found[0])
        return _redirect(
            key_msg=f"{label} 키를 저장했습니다. 다음 작업부터 씁니다", **_check(name, found)
        )

    @app.post("/settings/keys/{name}/check", dependencies=[Depends(check_csrf)])
    def verify_key(name: str) -> RedirectResponse:
        _known(name)
        q = _check(name)
        _record(name, q["key_check_code"])
        return _redirect(**q)

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
        key_watch.forget_key(state.repo, name)  # 날짜 메모·알림·자동 확인 상태도 같이(C53)
        log.info("API 키 삭제: %s", name)
        return _redirect(key_msg=f"{label} 키를 지웠습니다")
