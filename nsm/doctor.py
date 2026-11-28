"""`nsm doctor`: 환경 진단. 사람이 문제를 찾기 전에 기계가 먼저 찾는다."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from nsm.config.schema import AppConfig

Status = Literal["OK", "WARN", "FAIL"]

# 사용설명서·README는 "FAIL이 없으면 준비 끝"이라고 한다. 그 말이 참이 되게, 없으면 핵심 기능이 안 도는 것은
# FAIL이다(C61, ADR 0081): 설정 파일, Claude 키(원고를 못 만듦), 실계정일 때 Aside CLI와 게시 계정.
DRY_RUN_NOTE = "드라이런이라 지금은 필요 없음"


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


UNREADABLE = "unreadable"  # 설정 파일이 있는데 못 읽음: 실계정인지 알 수 없다


def _load() -> AppConfig | str | None:
    """설정을 한 번만 읽는다(C61 리뷰). 없으면 None, 못 읽으면 UNREADABLE."""
    from nsm.config.paths import config_path
    from nsm.config.store import load_config

    p = config_path()
    if not p.exists():
        return None
    try:
        return load_config(p)
    except Exception:  # noqa: BLE001  이유는 _config가 보여 준다
        return UNREADABLE


def _config(loaded: AppConfig | str | None = None) -> Check:
    """loaded: run_checks가 읽어 둔 설정(읽었으면 다시 읽지 않음). 못 읽었으면 이유를 보이려고 한 번 더."""
    from nsm.config.paths import config_path

    p = config_path()
    if not p.exists():
        return Check("설정 파일", "FAIL", f"{p} 없음. `nsm init`을 실행하세요")
    if isinstance(loaded, AppConfig):
        return Check("설정 파일", "OK", str(p))
    try:
        from nsm.config.store import load_config

        load_config(p)
        return Check("설정 파일", "OK", str(p))
    except Exception as e:  # noqa: BLE001
        return Check("설정 파일", "FAIL", str(e))


def _secrets() -> Check:
    try:
        from nsm.config.secrets import KEYS, KeyringSecretStore

        store = KeyringSecretStore()
        have = [k for k in KEYS if store.get(k)]
        missing = [k for k in KEYS if k not in have]
        detail = f"있음: {have or '-'} / 없음: {missing or '-'}"
        if "anthropic_api_key" not in have:
            # Claude 키가 없으면 원고가 하나도 만들어지지 않는다(C61): 선택 기능이 아니다
            return Check("API 키", "FAIL", f"{detail}. Claude 키가 없으면 원고를 만들 수 없습니다")
        return Check("API 키", "OK", detail)
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


def _live(cfg: AppConfig | str | None) -> bool:
    """실계정 모드인가. 설정이 없으면(nsm init 전) 드라이런으로 본다(설정 파일 점검이 따로 FAIL을 낸다)."""
    return isinstance(cfg, AppConfig) and not cfg.settings.dry_run


UNKNOWN_NOTE = "설정 파일을 읽지 못해 실계정인지 알 수 없음(위 설정 파일 줄을 먼저 고치세요)"


def _aside(cfg: AppConfig | str | None = None) -> Check:
    from nsm.adapters.drivers import find_aside_cli

    exe = find_aside_cli()
    if exe is None and cfg == UNREADABLE:
        return Check("Aside CLI", "WARN", f"없음. {UNKNOWN_NOTE}")
    if exe is None:
        if _live(cfg):
            return Check(
                "Aside CLI",
                "FAIL",
                "없음. 실계정(dry_run: false)은 Aside로 게시합니다(ADR 0017): https://docs.aside.com/help/developers",
            )
        return Check(
            "Aside CLI",
            "WARN",
            f"없음. {DRY_RUN_NOTE}. 실계정에 필요: https://docs.aside.com/help/developers",
        )
    # 카드 PNG는 일회용 로컬 서버로 넘기므로(ADR 0020) Aside 파일 권한 설정은 필요 없다
    return Check("Aside CLI", "OK", f"{exe} · 게시할 계정으로 Aside 브라우저에 로그인돼 있어야 함")


def _accounts(cfg: AppConfig | str | None) -> Check:
    if cfg == UNREADABLE:
        return Check("게시 계정", "WARN", UNKNOWN_NOTE)
    if not _live(cfg):
        return Check("게시 계정", "OK" if cfg else "WARN", DRY_RUN_NOTE)
    assert isinstance(cfg, AppConfig)
    s = cfg.settings
    have = {p: h for p, h in s.accounts.items() if h.strip()}
    if not have:
        return Check(
            "게시 계정",
            "FAIL",
            "실계정(dry_run: false)인데 settings.accounts가 비어 있어 아무 데도 게시하지 않습니다(사용설명서 5절)",
        )
    missing = [p for p in s.enabled_platforms if p not in have]
    detail = ", ".join(f"{p} {h}" for p, h in have.items())
    if missing:
        return Check("게시 계정", "WARN", f"{detail} · 계정 없음(게시 안 함): {', '.join(missing)}")
    return Check("게시 계정", "OK", detail)


CHECKS = [_python, _duckdb, _secrets, _chromium, _fonts]


def run_checks() -> list[Check]:
    cfg = _load()
    py, duck, *rest = (c() for c in CHECKS)
    return [py, duck, _config(cfg), *rest, _aside(cfg), _accounts(cfg)]
