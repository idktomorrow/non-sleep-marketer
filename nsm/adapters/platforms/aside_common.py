"""Aside 기반 실계정 어댑터의 공통 부분 (배치 G).

- 게시 직전마다 로그인된 핸들을 읽어 `settings.accounts`의 핸들과 비교한다. 다르면 게시하지 않는다
  (CLAUDE.md 가드레일: 실계정은 day1-verification.md의 테스트 계정에만).
- JS 본문은 이 모듈들의 상수이고, 바뀌는 값(문구, 핸들, URL)은 ARGS로만 들어간다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
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


class RateLimitedError(AdapterError):
    """플랫폼이 요청이 많다고 막음(HTTP 429). `retry_at`까지 이 플랫폼에 브라우저 요청을 보내지 않는다.

    로그인 문제가 아니므로 health 잡이 플랫폼을 멈추거나 "로그인 필요"를 알리지 않는다(ADR 0038).
    엔진은 이 클래스를 몰라도 되게 `retry_at` 속성으로 알아본다(포트는 동결).
    """

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


# 처음 막히면 2시간 쉬고, 쉰 뒤 바로 또 막히면 두 배씩(최대 8시간). 성공하면 처음으로.
RATE_LIMIT_COOLDOWN = timedelta(hours=2)
RATE_LIMIT_MAX = timedelta(hours=8)
# 예외 메시지로만 알 수 있는 경우. ERR_HTTP_RESPONSE_CODE_FAILURE는 404·500에도 나와 넣지 않는다
_RATE_LIMIT_TEXT = re.compile(r"HTTP ERROR 429|Too Many Requests")


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
        # 요청 제한으로 쉬는 중이면 그 끝 시각. 엔진(publish·reply)이 미리 보고 건너뛴다.
        self.blocked_until: datetime | None = None
        self._strikes = 0

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
        return self._call(body, {"handle": self.handle, **(args or {})}, files)

    def _call(self, body: str, args: dict, files: list[Path] | None = None, *, probe: bool = False):
        """브라우저 요청 하나. 요청 제한으로 쉬는 중이면 브라우저를 열지 않고 바로 RateLimitedError.

        probe(로그인 점검): 홈 화면은 글 페이지가 막혀도 열리므로, 성공해도 백오프를 초기화하지 않는다.
        """
        now = self._now()
        if self.blocked_until and now < self.blocked_until:
            raise RateLimitedError(
                f"{self.platform} 요청 제한으로 쉬는 중(HTTP 429)", self.blocked_until
            )
        try:
            data = self.repl.run(body, args, files)
        except AsideError as e:
            if e.code == "ratelimit" or _RATE_LIMIT_TEXT.search(str(e)):
                raise self._rate_limited(now, e) from e
            if e.code == "login":
                raise LoginRequiredError(f"{self.platform} 로그인 필요: {e}") from e
            if e.code == "mismatch":
                raise AccountMismatchError(f"{self.platform}: {e}") from e
            raise AdapterError(f"{self.platform}: {e}") from e
        if not probe:
            self._strikes = 0
        return data

    def _rate_limited(self, now: datetime, e: Exception) -> RateLimitedError:
        self._strikes += 1
        wait = min(RATE_LIMIT_COOLDOWN * 2 ** (self._strikes - 1), RATE_LIMIT_MAX)
        self.blocked_until = now + wait
        return RateLimitedError(f"{self.platform} 요청 제한: {e}", self.blocked_until)

    def _now(self) -> datetime:
        return self.clock.now()


# JS 공통: 현재 로그인 핸들 확인. 각 플랫폼 스크립트 앞에 붙인다.
JS_ASSERT_HANDLE = r"""
// 페이지가 아예 안 열린 경우를 로그인 풀림과 구분한다. 요청 제한(429) 화면에는 내 프로필 사진이 없어
// 예전에는 "로그인돼 있지 않음"으로 잘못 보고했다(2026-10-14~15 인스타그램 댓글 확인).
async function nsmCheckPage(pg) {
  const url = pg.url();
  const text = await pg.evaluate(() => ((document.body && document.body.innerText) || '').slice(0, 600)).catch(() => '');
  if (/HTTP ERROR 429|Too Many Requests|몇 분 후 다시 시도|Please wait a few minutes/i.test(text)) {
    throw new NsmError('ratelimit', '요청이 너무 많음(HTTP 429)');
  }
  if (url.startsWith('chrome-error://')) {
    const m = text.match(/(HTTP ERROR \d+|ERR_[A-Z_]+)/);
    throw new NsmError('page', '페이지를 열 수 없음(' + (m ? m[1] : url) + ')');
  }
}
async function nsmAssertHandle(pg, readHandle) {
  await nsmCheckPage(pg);
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
// 아이콘 버튼 찾기: svg의 aria-label 또는 <title>이 name인 것 중 scope 안의 nth번째 버튼에 표시를 단다.
// scope 선택자에 맞는 요소가 여럿이면(예: 알림 안내 대화상자 + 업로드 대화상자) 전부 본다(2026-10-18 인스타그램).
async function nsmMarkIcon(pg, scopeSel, name, nth) {
  const key = 'k' + Math.random().toString(36).slice(2);
  const ok = await pg.evaluate(([scopeSel, name, nth, key]) => {
    const scopes = scopeSel ? Array.from(document.querySelectorAll(scopeSel)) : [document];
    if (!scopes.length) return false;
    const svgs = scopes.flatMap(sc => Array.from(sc.querySelectorAll('svg')));
    const hits = svgs.filter(s => {
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
// 플랫폼이 띄우는 안내 대화상자("알림 설정 → 나중에 하기" 등)를 닫는다. 안내 창이 앞에 겹치면
// 업로드 대화상자 안의 버튼을 찾지 못한다(2026-10-18 인스타그램 "자르기 단계가 나오지 않음"). 닫은 수를 돌려준다.
const NSM_DISMISS = ['나중에 하기', 'Not Now', '나중에', '취소하기'];
async function nsmDismissPrompts(pg) {
  let n = 0;
  for (let round = 0; round < 3; round++) {
    const clicked = await pg.evaluate((labels) => {
      for (const d of document.querySelectorAll('[role="dialog"]')) {
        const heading = (d.querySelector('h1, h2, h3') || {}).textContent || '';
        if (!/알림|Notification/i.test(heading)) continue;
        for (const b of d.querySelectorAll('button, [role="button"]')) {
          if (labels.includes((b.innerText || '').trim())) { b.click(); return true; }
        }
      }
      return false;
    }, NSM_DISMISS);
    if (!clicked) break;
    n++;
    await sleep(600);
  }
  return n;
}
async function nsmUntil(fn, ms, step) {
  const end = Date.now() + Math.min(ms || 30000, nsmLeft());
  while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(step || 1000); }
  return null;
}
"""
