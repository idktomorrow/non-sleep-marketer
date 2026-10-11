"""Playwright 영구 프로필 드라이버. 사람이 한 번 로그인한 상태가 프로필 폴더에 남는다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nsm.adapters.drivers.base import DriverUnavailableError, Link

_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  let block = a.closest('li, article, section, div');
  let ctx = block ? block.innerText : a.innerText;
  return {text: (a.innerText || '').trim(), href: a.href, context: (ctx || '').trim().slice(0, 400)};
})
"""


class PlaywrightDriver:
    def __init__(self, profile_dir: Path, headless: bool = True, timeout_ms: int = 20000) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None

    def _ensure(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise DriverUnavailableError("playwright가 설치돼 있지 않습니다") from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, locale="ko-KR"
            )
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise DriverUnavailableError(
                f"Chromium을 띄우지 못했습니다. `uv run playwright install chromium`을 확인하세요: {e}"
            ) from e
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(self.timeout_ms)
        return self._page

    def goto(self, url: str) -> None:
        page = self._ensure()
        page.goto(url, wait_until="domcontentloaded")

    def links(self) -> list[Link]:
        page = self._ensure()
        return [Link(**d) for d in page.evaluate(_LINKS_JS)]

    def page_text(self) -> str:
        return str(self._ensure().inner_text("body"))

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:  # noqa: BLE001  브라우저가 이미 죽었어도 Playwright는 멈춘다
            pass
        finally:
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:  # noqa: BLE001  종료 시점(다른 스레드일 수 있음)에는 최선만 다한다
                pass
            finally:
                self._ctx = self._pw = self._page = None
