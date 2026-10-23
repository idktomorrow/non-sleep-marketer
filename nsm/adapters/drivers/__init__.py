from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from nsm.adapters.drivers.base import BrowserDriver, DriverUnavailableError, Link

log = logging.getLogger(__name__)


def find_aside_cli() -> Path | None:
    """Aside CLI 실행 파일. 기본 설치 위치를 먼저, 없으면 PATH.

    Windows에서는 .exe만: .cmd/.bat 래퍼는 cmd.exe가 인자를 다시 해석해 스크립트가 깨지거나 주입될 수 있어 쓰지 않는다.
    macOS·리눅스는 확장자 없는 실행 파일(예: ~/.local/bin/aside → Aside CLI.app 안의 aside)을 그대로 쓴다.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = Path(local) / "Aside" / "CLI" / "current" / "aside.exe"
        if p.exists():
            return p
    found = shutil.which("aside")
    if not found:
        return None
    if os.name == "nt":
        return Path(found) if Path(found).suffix.lower() == ".exe" else None
    return Path(found) if os.access(found, os.X_OK) else None


def make_driver(kind: str, profile_dir: Path, headless: bool) -> BrowserDriver:
    if kind == "aside":
        # ADR 0021: 수집은 Playwright 수집 전용 프로필을 쓴다(Aside 브라우저에 검색 탭이 열리는 불편,
        # 수집은 로그인 불필요). browser_driver=aside는 실계정 게시·댓글(Aside repl)을 뜻한다.
        kind = "playwright"
    if kind == "playwright":
        from nsm.adapters.drivers.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(profile_dir, headless=headless)
    raise DriverUnavailableError(f"알 수 없는 드라이버: {kind}")


__all__ = ["BrowserDriver", "DriverUnavailableError", "Link", "find_aside_cli", "make_driver"]
