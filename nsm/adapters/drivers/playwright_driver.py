"""Playwright 영구 프로필 드라이버. 사람이 한 번 로그인한 상태가 프로필 폴더에 남는다."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nsm.adapters.drivers.base import LINK_CONTEXT_JS, DriverUnavailableError, Link

_LINKS_JS = (
    "() => { const ctxOf = "
    + LINK_CONTEXT_JS.strip()
    + "; return Array.from(document.querySelectorAll('a[href]')).map(a => "
    "({text: (a.innerText || '').trim(), href: a.href, context: ctxOf(a)})); }"
)


# 본문 칸: 알려진 본문 선택자 → article/main → 문단이 가장 많은 칸. innerText만 돌려준다.
_ARTICLE_JS = """
() => {
  const known = ['#dic_area', '#newsct_article', '.se-main-container', '#postViewArea',
                 '.post_ct', '#viewTypeSelector', 'article', 'main', '[role="main"]'];
  for (const sel of known) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || '').trim().length > 200) return el.innerText;
  }
  let best = null, score = 0;
  for (const el of document.querySelectorAll('div, section')) {
    const ps = el.querySelectorAll(':scope > p, :scope > div > p').length;
    if (ps > score) { score = ps; best = el; }
  }
  return (best || document.body).innerText;
}
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

    def article_text(self) -> str:
        """본문으로 보이는 칸의 글(메뉴·댓글·추천 글을 덜 섞음)."""
        return str(self._ensure().evaluate(_ARTICLE_JS))

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
