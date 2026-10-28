"""API 키 보관. 파일에 두지 않고 OS 자격 증명 저장소(Windows 자격 증명 관리자)에 둔다.

조회 순서: 환경 변수 → keyring. 환경 변수는 CI·임시 실행용이다.
"""

from __future__ import annotations

import contextlib
import os
from typing import Protocol

SERVICE = "non-sleep-marketer"
KEYS = (
    "anthropic_api_key",
    "gemini_api_key",
    "openai_api_key",
)  # 화면·마법사 순서(필수 → 무료 → 선택)
# 키 이름과 쓰임: 마법사(nsm init)와 설정 화면(C35)이 같이 쓴다
KEY_NAMES = {"anthropic_api_key": "Claude", "gemini_api_key": "Gemini", "openai_api_key": "OpenAI"}
KEY_USES = {
    "anthropic_api_key": "원고·답글을 씁니다. 필수",
    "gemini_api_key": "자료·댓글을 분류합니다. 무료 구간(없으면 Claude로 분류)",
    "openai_api_key": "표지 배경 이미지. 선택이며 기본은 꺼짐(설정 image_generation)",
}
ENV_NAMES = {
    "anthropic_api_key": ("NSM_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    "openai_api_key": ("NSM_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "gemini_api_key": (
        "NSM_GEMINI_API_KEY",
        "GEMINI_API_KEY",
    ),  # GOOGLE_API_KEY는 다른 구글 키일 수 있어 안 읽음
}


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def is_env(self, key: str) -> bool: ...  # 환경 변수 키는 get이 먼저 돌려주고 delete로 못 지운다


def _from_env(key: str) -> str | None:
    for name in ENV_NAMES.get(key, ()):
        v = os.environ.get(name)
        if v:
            return v
    return None


class KeyringSecretStore:
    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def get(self, key: str) -> str | None:
        env = _from_env(key)
        if env:
            return env
        import keyring

        try:
            return keyring.get_password(self.service, key)
        except Exception:  # noqa: BLE001  (백엔드 없는 환경)
            return None

    def is_env(self, key: str) -> bool:
        """환경 변수로 들어온 키인가. 프로그램은 환경 변수를 지울 수 없다(설정 화면 안내, C35)."""
        return _from_env(key) is not None

    def set(self, key: str, value: str) -> None:
        import keyring

        keyring.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        import keyring

        with contextlib.suppress(Exception):  # 없는 키를 지우면 예외: 결과는 부른 쪽이 get으로 확인
            keyring.delete_password(self.service, key)


class MemorySecretStore:
    """테스트용."""

    def __init__(
        self, values: dict[str, str] | None = None, env: dict[str, str] | None = None
    ) -> None:
        self.values = dict(values or {})
        self.env = dict(env or {})  # 환경 변수 흉내: get에는 보이고 delete로는 안 지워진다

    def is_env(self, key: str) -> bool:
        return key in self.env

    def get(self, key: str) -> str | None:
        return self.env.get(key) or self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def mask(value: str | None) -> str:
    if not value:
        return "(없음)"
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "****"
