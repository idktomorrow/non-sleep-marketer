from __future__ import annotations

from pathlib import Path

from nsm.adapters.drivers.base import BrowserDriver, DriverUnavailableError, Link


def make_driver(kind: str, profile_dir: Path, headless: bool) -> BrowserDriver:
    if kind == "playwright":
        from nsm.adapters.drivers.playwright_driver import PlaywrightDriver

        return PlaywrightDriver(profile_dir, headless=headless)
    if kind == "aside":
        # ADR 0010: Aside 드라이버는 배치 B(1일차 검증)에서 CLI 동작을 확인한 뒤 배치 G에서 구현한다.
        raise DriverUnavailableError(
            "Aside 드라이버는 아직 구현되지 않았습니다(ADR 0010). config.yaml의 "
            "settings.browser_driver를 playwright로 두세요."
        )
    raise DriverUnavailableError(f"알 수 없는 드라이버: {kind}")


__all__ = ["BrowserDriver", "DriverUnavailableError", "Link", "make_driver"]
