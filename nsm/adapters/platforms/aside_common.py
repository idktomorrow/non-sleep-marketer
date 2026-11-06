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
from nsm.adapters.platforms.auth_state import (
    EVIDENCE_STATES,
    classify,
    is_blank,
    js_auth_args,
    looks_logged_in,
)
from nsm.config import paths
from nsm.domain.models import HealthStatus, Platform
from nsm.ports import AdapterError, Clock, NotSupportedError


class PlatformError(AdapterError):
    """종류(`kind`)가 붙은 어댑터 실패(E1, ADR 0054). 엔진은 이 클래스를 몰라도 `kind` 속성으로 표를 읽는다
    (포트는 동결). `evidence_dir`: 실패 순간 남긴 증거 폴더."""

    kind = "transient"

    def __init__(self, message: str, kind: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        if kind:
            self.kind = kind
        self.evidence_dir: str | None = None
        # Aside 스크립트의 오류 코드('page' = 페이지가 안 열림·오류 화면). 엔진이 속성으로 읽는다(C37)
        self.code = code


class LoginRequiredError(PlatformError):
    """로그인이 풀려 있음. 이 호출은 일반 실패로 처리되고, 30분마다 도는 health 잡이 알림을 보내고 그 플랫폼을 멈춘다.

    `auth_state`: 무엇이 막고 있나(logged_out·challenge·verify·two_factor·restricted·unknown, E5 ADR 0060).
    화면 정보가 없으면 None(예전처럼 "로그인 필요"로만 보인다).
    엔진은 이 클래스를 몰라도 `auth_state` 속성으로 읽는다(포트는 동결, `kind`와 같은 방식).
    """

    kind = "auth"

    def __init__(self, message: str, auth_state: str | None = None) -> None:
        super().__init__(message)
        self.auth_state = auth_state


class AccountMismatchError(PlatformError):
    """로그인된 계정이 설정의 게시 계정과 다름. 게시하지 않는다."""

    kind = "auth"


class RateLimitedError(PlatformError):
    """플랫폼이 요청이 많다고 막음(HTTP 429). `retry_at`까지 이 플랫폼에 브라우저 요청을 보내지 않는다.

    로그인 문제가 아니므로 health 잡이 플랫폼을 멈추거나 "로그인 필요"를 알리지 않는다(ADR 0038).
    엔진은 이 클래스를 몰라도 되게 `retry_at` 속성으로 알아본다(포트는 동결).
    """

    kind = "rate_limited"

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


# 처음 막히면 2시간 쉬고, 쉰 뒤 바로 또 막히면 두 배씩(최대 8시간). 성공하면 처음으로.
RATE_LIMIT_COOLDOWN = timedelta(hours=2)
RATE_LIMIT_MAX = timedelta(hours=8)
# 예외 메시지로만 알 수 있는 경우. ERR_HTTP_RESPONSE_CODE_FAILURE는 404·500에도 나와 넣지 않는다
_RATE_LIMIT_TEXT = re.compile(r"HTTP ERROR 429|Too Many Requests")
# Aside 스크립트의 오류 코드 → 오류 종류(E1). 'ui'는 스크립트가 직접 던진 "버튼·메뉴·대화상자를 못 찾음"
# (화면 변경)뿐이다. 라벨 없는 예외('script': 탭이 닫힘, 실행 맥락 사라짐, net::ERR_…, 대기 시간 초과)는
# 다시 하면 될 수 있어 일시적으로 본다(지금처럼 재시도)
_KIND_BY_CODE = {
    "ui": "ui_changed",
    "login": "auth",
    "mismatch": "auth",
    "ratelimit": "rate_limited",
}
EVIDENCE_EVERY = timedelta(
    hours=1
)  # 같은 플랫폼·같은 종류의 증거는 한 시간에 한 번(health·댓글 묶음 반복)


def kind_for_code(code: str, message: str = "") -> str:
    return _KIND_BY_CODE.get(code, "transient")


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
            raise PlatformError(
                f"올릴 수 없는 파일: {Path(p).name} (NSM media 폴더의 PNG만)", "config_or_bug"
            )
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
        self._evidence_at: dict[str, datetime] = {}  # 종류 → 마지막으로 증거를 남긴 시각
        # 마지막 health가 본 로그인 세부 상태(E5). 로그인돼 있으면 None. 엔진이 속성으로 읽는다(포트 동결)
        self.auth_state: str | None = None
        # 마지막 health가 "로그인은 됐는데 계정 이름을 못 읽음"(화면 변경)을 봤으면 {"detail", "evidence_dir"}.
        # 멈추지 않고 health 잡이 화면 변경으로 기록·알린다(C39, ADR 0064). 엔진이 속성으로 읽는다(포트 동결)
        self.ui_warning: dict[str, str | None] | None = None

    def capabilities(self) -> set[str]:
        return set(PLATFORM_CAPABILITIES[self.platform])

    def _need(self, cap: str) -> None:
        if cap not in self.capabilities():
            raise NotSupportedError(f"{self.platform}: {cap} 미지원")

    def _run(self, body: str, args: dict | None = None, files: list[Path] | None = None):
        if not self.handle:
            raise AccountMismatchError(
                f"{self.platform} 게시 계정이 설정에 없습니다(settings.accounts). 실계정 동작을 하지 않습니다.",
                "config_or_bug",  # 로그인 문제가 아니라 설정 누락
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
        # 로그인된 화면 표지 규칙(C39): nsmAssertHandle이 핸들을 못 읽었을 때 화면 변경인지 가른다
        args = {**args, "nsm_auth": js_auth_args(self.platform)}
        try:
            data = self.repl.run(body, args, files)
        except AsideError as e:
            err = self._classify(now, e)
            if err.kind != "rate_limited" and e.code != "interrupted":
                self._attach_evidence(err, e)
            raise err from e
        if not probe:
            self._strikes = 0
        return data

    def _classify(self, now: datetime, e: AsideError) -> PlatformError:
        """Aside 오류 → 종류가 붙은 어댑터 오류(E1). 문구는 예전과 같다."""
        if e.code == "ratelimit" or _RATE_LIMIT_TEXT.search(str(e)):
            return self._rate_limited(now, e)
        if e.code == "login":
            ev = getattr(e, "evidence", None)
            ev = ev if isinstance(ev, dict) else None
            if is_blank(
                ev
            ):  # 글 주소인데 하얀 화면: 로그인 풀림이 아니라 덜 열림(2026-11-02 스레드)
                return self._blank_error()
            return LoginRequiredError(
                f"{self.platform} 로그인 필요: {e}", classify(ev) if ev else None
            )
        if e.code == "mismatch":
            return AccountMismatchError(f"{self.platform}: {e}")
        return PlatformError(f"{self.platform}: {e}", kind_for_code(e.code, str(e)), e.code)

    def _attach_evidence(self, err: PlatformError, e: AsideError) -> None:
        """실패 증거를 남기고 오류 문구 끝에 폴더 이름을 붙인다(전체 경로는 `evidence_dir`).
        저장이 실패해도 원래 오류는 그대로. 같은 종류는 한 시간에 한 번만 남긴다."""
        d = self._save_evidence(err.kind, getattr(e, "evidence", None), str(err))
        if d is not None:
            err.evidence_dir = str(d)
            # 집 폴더 경로가 알림·대시보드에 드러나지 않게 이름만(logs\\shots 아래)
            err.args = (f"{err.args[0]} (증거: {d.name})", *err.args[1:])

    def _blank_error(self) -> PlatformError:
        return PlatformError(
            f"{self.platform}: 페이지가 덜 열림(빈 화면, 로그인 여부 모름)", "transient", "page"
        )

    def _evidence_open(self, kind: str) -> bool:
        last = self._evidence_at.get(kind)
        return last is None or self._now() - last >= EVIDENCE_EVERY

    def _save_evidence(self, kind: str, evidence: dict | None, error: str) -> Path | None:
        """증거 폴더 하나(같은 종류는 한 시간에 한 번). 못 남기면 None."""
        from nsm.adapters.evidence import save_evidence

        now = self._now()
        if not self._evidence_open(kind):
            return None
        d = save_evidence(
            self.platform,
            now,
            evidence,
            error=error,
            kind=kind,
            log_file=paths.logs_dir() / "nsm.log",
        )
        if d is not None:
            self._evidence_at[kind] = now
        return d

    def _health(self, body: str, name: str) -> HealthStatus:
        """로그인 점검(health) 공통(E5). 스크립트는 홈 화면 하나만 열고, 로그인이 안 보이면 그 화면 정보(page)를
        함께 돌려준다. 그걸로 세부 상태를 `auth_state`에 두고, 보안 확인·제한이면 증거를 남긴다(예외가 없어도).

        - 하얀 빈 화면은 로그인 풀림이 아니라 일시적 실패로 던진다(연속일 때만 멈춤).
        - 로그인 페이지를 새로 열거나 확인 창을 누르지 않는다. 자동으로 풀지 않는다(ADR 0060).
        """
        self.ui_warning = None
        # 스크린샷은 증거를 남길 수 있을 때만 찍게 한다(한 시간 쿨다운 안이면 화면 글만)
        shot = self._evidence_open("auth") or self._evidence_open("ui_changed")
        data = self._call(body, {"shot": shot}, probe=True) or {}
        me, page = data.get("me", ""), data.get("page")
        page = page if isinstance(page, dict) else None
        if not me:
            if is_blank(page):
                self.auth_state = None
                raise self._blank_error()
            if looks_logged_in(page, self.platform):
                # 로그인 표지가 보이는데 핸들만 못 읽음: 로그인 문제가 아니라 화면 변경(C39, ADR 0064).
                # 멈추지 않는다. 게시·답글은 행동 직전 nsmAssertHandle이 계정을 확인하지 못해 그 건만 실패한다
                self.auth_state = None
                detail = f"{name} 로그인은 됐으나 계정 이름을 못 읽음(화면 변경)"
                d = self._save_evidence(
                    "ui_changed", page, f"{self.platform} 로그인 확인: {detail}"
                )
                self.ui_warning = {"detail": detail, "evidence_dir": str(d) if d else None}
                return HealthStatus(logged_in=True, detail=detail)
            self.auth_state = classify(page) if page else None
            detail = f"{name} 로그인 필요(Aside 브라우저)"
            if self.auth_state in EVIDENCE_STATES:
                d = self._save_evidence(
                    "auth", page, f"{self.platform} 로그인 확인: {self.auth_state}"
                )
                if d is not None:
                    detail += f" (증거: {d.name})"
            return HealthStatus(logged_in=False, detail=detail)
        self.auth_state = None
        if self.handle and me != self.handle:
            return HealthStatus(logged_in=False, detail=f"로그인 계정 @{me} ≠ 설정 @{self.handle}")
        return HealthStatus(logged_in=True, detail=f"@{me}")

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
  // 플랫폼 오류 화면("문제가 발생했습니다. 나중에 다시 시도해보세요." + "다시 시도" 버튼, 2026-11-04 스레드, C37).
  // 안내문과 "다시 시도" 버튼이 함께 있을 때만 본다: 댓글·글 본문에 같은 말이 있어도 버튼은 만들 수 없다.
  // 로그인 폼·보안 확인 화면에는 이 안내문이 없다. 빈 화면(E5)보다 먼저 걸린다(로그인 확인 전에 부르므로).
  if (await nsmErrorScreen(pg)) throw new NsmError('page', '플랫폼이 오류 화면을 띄움');
}
const NSM_ERROR_TEXT = /문제가 발생했습니다|Something went wrong|나중에 다시 시도|Try again later/i;
async function nsmErrorScreen(pg) {
  return await pg.evaluate((src) => {
    const re = new RegExp(src, 'i');
    if (!re.test((document.body && document.body.innerText) || '')) return false;
    // 버튼에서 몇 칸 위까지 올라가 같은 칸에 안내문이 있을 때만(페이지 다른 곳의 문구·버튼을 엮지 않게)
    return Array.from(document.querySelectorAll('[role="button"], button')).some(b => {
      if (!/^(다시 시도|Try again|Retry)$/i.test((b.innerText || '').trim())) return false;
      let box = b.parentElement;
      for (let i = 0; i < 4 && box; i++, box = box.parentElement) if (re.test(box.innerText || '')) return true;
      return false;
    });
  }, NSM_ERROR_TEXT.source).catch(() => false);
}
// 반드시 있어야 하는 요소(글 본문 등)를 nsmUntil로 기다린 결과가 비었으면 빈 결과로 넘기지 않는다(C37).
// 오류 화면이면 그 오류, 아니면 code(기본 'page': 일시적)로 던진다. 있으면 그 값을 그대로 돌려준다.
async function nsmEnsure(pg, v, msg, code) {
  if (v) return v;
  await nsmCheckPage(pg);
  throw new NsmError(code || 'page', msg);
}
// 기다리기 + 확인을 한 번에: 반드시 있어야 하는 요소는 nsmUntil 대신 이것을 쓴다(빈 결과로 넘어가지 않게)
async function nsmNeed(pg, fn, ms, msg, code) {
  return await nsmEnsure(pg, await nsmUntil(fn, ms), msg, code);
}
async function nsmAssertHandle(pg, readHandle) {
  await nsmCheckPage(pg);
  const me = ((await readHandle(pg)) || '').replace(/^@/, '').toLowerCase();
  if (!me) {
    await nsmCheckPage(pg);  // 기다리는 사이 오류 화면이 떴으면 로그인 문제가 아니다(C37)
    // 로그인한 사람만 보는 표지가 있고 로그인 폼·보안 확인이 없으면 화면 변경(C39). 계정을 확인하지 못했으니
    // 이 동작은 하지 않는다(행동 직전엔 핸들 필수, ADR 0064)
    if (await nsmLoggedInScreen(pg)) throw new NsmError('ui', '로그인은 됐으나 계정 이름을 못 읽음(화면 변경)');
    throw new NsmError('login', '로그인돼 있지 않음');
  }
  if (me !== ARGS.handle) throw new NsmError('mismatch', '로그인 계정 @' + me + ' ≠ 설정 @' + ARGS.handle);
  return me;
}
// 규칙(정규식 원문)은 Python이 ARGS.nsm_auth로 넘긴다(data/auth_states.json 한 곳, auth_state.js_auth_args)
async function nsmLoggedInScreen(pg) {
  const a = ARGS.nsm_auth;
  if (!a || !a.markers) return false;
  if (new RegExp(a.check_urls, 'i').test(pg.url() || '')) return false;
  const seen = await pg.evaluate(() => {
    const labels = [];
    for (const el of document.querySelectorAll('svg title, [aria-label]')) {
      const t = (el.tagName.toLowerCase() === 'title' ? el.textContent : el.getAttribute('aria-label')) || '';
      if (t.trim() && labels.length < 200) labels.push(t.trim());
    }
    return [((document.body && document.body.innerText) || '').slice(0, 4000).trim(), ...labels].join('\n');
  }).catch(() => '');
  return new RegExp(a.markers, 'i').test(seen) && !new RegExp(a.blockers, 'i').test(seen);
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
    // 겹치는 scope(안쪽 요소도 선택자에 맞는 경우)에서 같은 svg가 두 번 세어지지 않게 중복을 뺀다
    const svgs = Array.from(new Set(scopes.flatMap(sc => Array.from(sc.querySelectorAll('svg')))));
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
        // 제목 요소가 없으면(div로 그린 제목) 첫 줄 글을 제목으로 본다
        const h = d.querySelector('h1, h2, h3, [role="heading"]');
        const heading = h ? (h.textContent || '') : ((d.innerText || '').trim().split('\n')[0] || '');
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
