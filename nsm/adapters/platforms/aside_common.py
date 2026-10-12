"""Aside 기반 실계정 어댑터의 공통 부분 (배치 G).

- 게시 직전마다 로그인된 핸들을 읽어 `settings.accounts`의 핸들과 비교한다. 다르면 게시하지 않는다
  (CLAUDE.md 가드레일: 실계정은 day1-verification.md의 테스트 계정에만).
- JS 본문은 이 모듈들의 상수이고, 바뀌는 값(문구, 핸들, URL)은 ARGS로만 들어간다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nsm.adapters.drivers.aside import AsideError, AsideRepl
from nsm.adapters.platforms import PLATFORM_CAPABILITIES
from nsm.config import paths
from nsm.domain.models import Platform
from nsm.ports import AdapterError, Clock, NotSupportedError


class LoginRequiredError(AdapterError):
    """로그인이 풀려 있음. 이 호출은 일반 실패로 처리되고, 30분마다 도는 health 잡이 알림을 보내고 그 플랫폼을 멈춘다."""


class AccountMismatchError(AdapterError):
    """로그인된 계정이 설정의 게시 계정과 다름. 게시하지 않는다."""


def norm_handle(h: str | None) -> str:
    return (h or "").strip().lstrip("@").lower()


def media_root() -> Path:
    return (paths.nsm_home() / "media").resolve()


def check_upload_paths(images: list[Path], root: Path | None = None) -> list[Path]:
    """앱이 만든 카드 PNG만 올린다: NSM media 폴더 아래의 .png만 허용."""
    root = (root or media_root()).resolve()
    out = []
    for p in images:
        rp = Path(p).resolve()
        if rp.suffix.lower() != ".png" or not rp.is_relative_to(root) or not rp.is_file():
            raise AdapterError(f"올릴 수 없는 파일: {Path(p).name} (NSM media 폴더의 PNG만)")
        out.append(rp)
    return out


class AsideAdapterBase:
    platform: Platform

    def __init__(
        self,
        handle: str | None,
        clock: Clock,
        repl: AsideRepl | None = None,
        media_dir: Path | None = None,
    ) -> None:
        self.handle = norm_handle(handle)
        self.clock = clock
        self.repl = repl or AsideRepl()
        self.media_dir = media_dir

    def capabilities(self) -> set[str]:
        return set(PLATFORM_CAPABILITIES[self.platform])

    def _need(self, cap: str) -> None:
        if cap not in self.capabilities():
            raise NotSupportedError(f"{self.platform}: {cap} 미지원")

    def _run(self, body: str, args: dict | None = None, files: list[Path] | None = None):
        if not self.handle:
            raise AccountMismatchError(
                f"{self.platform} 게시 계정이 설정에 없습니다(settings.accounts). 실계정 동작을 하지 않습니다."
            )
        try:
            return self.repl.run(body, {"handle": self.handle, **(args or {})}, files)
        except AsideError as e:
            if e.code == "login":
                raise LoginRequiredError(f"{self.platform} 로그인 필요: {e}") from e
            if e.code == "mismatch":
                raise AccountMismatchError(f"{self.platform}: {e}") from e
            raise AdapterError(f"{self.platform}: {e}") from e

    def _now(self) -> datetime:
        return self.clock.now()


# JS 공통: 현재 로그인 핸들 확인. 각 플랫폼 스크립트 앞에 붙인다.
JS_ASSERT_HANDLE = r"""
async function nsmAssertHandle(pg, readHandle) {
  const me = ((await readHandle(pg)) || '').replace(/^@/, '').toLowerCase();
  if (!me) throw new NsmError('login', '로그인돼 있지 않음');
  if (me !== ARGS.handle) throw new NsmError('mismatch', '로그인 계정 @' + me + ' ≠ 설정 @' + ARGS.handle);
  return me;
}
async function nsmClickText(scope, text) {
  const cands = scope.locator('[role="button"], button, [role="menuitem"]');
  const n = await cands.count();
  for (let i = 0; i < n; i++) {
    const t = ((await cands.nth(i).innerText()) || '').trim();
    if (t === text) { await cands.nth(i).click(); return true; }
  }
  return false;
}
// 아이콘 버튼 찾기: svg의 aria-label 또는 <title>이 name인 것 중 scope 안의 nth번째 버튼에 표시를 단다
async function nsmMarkIcon(pg, scopeSel, name, nth) {
  const key = 'k' + Math.random().toString(36).slice(2);
  const ok = await pg.evaluate(([scopeSel, name, nth, key]) => {
    const scope = scopeSel ? document.querySelector(scopeSel) : document;
    if (!scope) return false;
    const hits = Array.from(scope.querySelectorAll('svg')).filter(s => {
      const t = s.querySelector('title');
      return s.getAttribute('aria-label') === name || (t && t.textContent === name);
    }).map(s => s.closest('[role="button"], button, a')).filter(Boolean);
    const el = hits[nth || 0];
    if (!el) return false;
    el.setAttribute('data-nsm-mark', key);
    return true;
  }, [scopeSel, name, nth || 0, key]);
  return ok ? pg.locator('[data-nsm-mark="' + key + '"]') : null;
}
async function nsmUntil(fn, ms, step) {
  const end = Date.now() + Math.min(ms || 30000, nsmLeft());
  while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(step || 1000); }
  return null;
}
"""
