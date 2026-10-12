from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from nsm.adapters.drivers.base import BrowserDriver, DriverUnavailableError, Link

log = logging.getLogger(__name__)


def find_aside_cli() -> Path | None:
    """Aside CLI 실행 파일. PATH에 없으면 Windows 기본 설치 위치를 본다."""
    found = shutil.which("aside")
    if found:
        return Path(found)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = Path(local) / "Aside" / "CLI" / "current" / "aside.exe"
        if p.exists():
            return p
    return None


def make_driver(kind: str, profile_dir: Path, headless: bool) -> BrowserDriver:
    if kind == "aside":
        # ADR 0017: 기본값은 Aside(1일차 검증에서 무인 게시 확인). 드라이버 구현은 배치 G.
        # 그 전까지는 Playwright로 대신해 수집이 멈추지 않게 한다.
        log.info("Aside 드라이버는 배치 G에서 구현됩니다. 지금은 Playwright로 대신합니다.")
        kind = "playwright"
    if kind == "playwright":
        from nsm.adapters.drivers.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(profile_dir, headless=headless)
    raise DriverUnavailableError(f"알 수 없는 드라이버: {kind}")


__all__ = ["BrowserDriver", "DriverUnavailableError", "Link", "find_aside_cli", "make_driver"]
