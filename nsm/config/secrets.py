"""API 키 보관. 파일에 두지 않고 OS 자격 증명 저장소(Windows 자격 증명 관리자)에 둔다.

조회 순서: 환경 변수 → keyring. 환경 변수는 CI·임시 실행용이다.
"""

from __future__ import annotations

import contextlib
import os
from typing import Protocol

SERVICE = "non-sleep-marketer"
KEYS = ("anthropic_api_key", "gemini_api_key", "openai_api_key")
ENV_NAMES = {
    "anthropic_api_key": ("NSM_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    "openai_api_key": ("NSM_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "gemini_api_key": ("NSM_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


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

    def set(self, key: str, value: str) -> None:
        import keyring

        keyring.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        import keyring

        with contextlib.suppress(Exception):
            keyring.delete_password(self.service, key)


class MemorySecretStore:
    """테스트용."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def mask(value: str | None) -> str:
    if not value:
        return "(없음)"
    return value[:4] + "…" + value[-2:] if len(value) > 8 else "****"
