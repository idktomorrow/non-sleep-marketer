"""`nsm doctor`: 환경 진단. 사람이 문제를 찾기 전에 기계가 먼저 찾는다."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

Status = Literal["OK", "WARN", "FAIL"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str


def _python() -> Check:
    ok = sys.version_info[:2] == (3, 12)
    return Check("Python 3.12", "OK" if ok else "FAIL", sys.version.split()[0])


def _duckdb() -> Check:
    try:
        from nsm.store.db import Database

        Database(":memory:").close()
        return Check("DuckDB", "OK", "메모리 DB 열기 성공")
    except Exception as e:  # noqa: BLE001
        return Check("DuckDB", "FAIL", str(e))


def _config() -> Check:
    from nsm.config.paths import config_path

    p = config_path()
    if not p.exists():
        return Check("설정 파일", "WARN", f"{p} 없음. `nsm init`을 실행하세요")
    try:
        from nsm.config.store import load_config

        load_config(p)
        return Check("설정 파일", "OK", str(p))
    except ModuleNotFoundError:
        return Check("설정 파일", "WARN", "설정 로더는 배치 C에서 추가됩니다")
    except Exception as e:  # noqa: BLE001
        return Check("설정 파일", "FAIL", f"{p}: {e}")


def _secrets() -> Check:
    try:
        from nsm.config.secrets import KEYS, KeyringSecretStore

        store = KeyringSecretStore()
        have = [k for k in KEYS if store.get(k)]
        missing = [k for k in KEYS if k not in have]
        # 글 공급자는 Claude 또는 OpenAI 중 하나면 된다(S13)
        status: Status = (
            "OK" if ("anthropic_api_key" in have or "openai_api_key" in have) else "WARN"
        )
        return Check("API 키", status, f"있음: {have or '-'} / 없음: {missing or '-'}")
    except ModuleNotFoundError:
        return Check("API 키", "WARN", "키 저장소는 배치 C에서 추가됩니다")
    except Exception as e:  # noqa: BLE001
        return Check("API 키", "WARN", f"keyring 확인 실패: {e}")


def _chromium() -> Check:
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
        if Path(exe).exists():
            return Check("Playwright Chromium", "OK", exe)
        return Check("Playwright Chromium", "WARN", "`uv run playwright install chromium` 필요")
    except Exception as e:  # noqa: BLE001
        return Check("Playwright Chromium", "WARN", str(e))


def _fonts() -> Check:
    from importlib import resources

    fonts = resources.files("nsm.templates").joinpath("fonts")
    found = [f.name for f in fonts.iterdir()] if fonts.is_dir() else []
    if any("Pretendard" in n for n in found):
        return Check("Pretendard 폰트", "OK", ", ".join(found))
    return Check("Pretendard 폰트", "WARN", "nsm/templates/fonts/ 비어 있음 (배치 S-α)")


def _aside() -> Check:
    from nsm.adapters.drivers import find_aside_cli

    exe = find_aside_cli()
    if exe is None:
        return Check(
            "Aside CLI",
            "WARN",
            "없음. 기본 브라우저 드라이버(ADR 0017)에 필요: https://docs.aside.com/help/developers",
        )
    # 카드 PNG는 일회용 로컬 서버로 넘기므로(ADR 0020) Aside 파일 권한 설정은 필요 없다
    return Check("Aside CLI", "OK", f"{exe} · 게시할 계정으로 Aside 브라우저에 로그인돼 있어야 함")


CHECKS = [_python, _duckdb, _config, _secrets, _chromium, _fonts, _aside]


def run_checks() -> list[Check]:
    return [c() for c in CHECKS]
