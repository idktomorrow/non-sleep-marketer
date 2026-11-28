"""스레드 실계정 어댑터: Aside 브라우저를 `aside repl`로 조작한다 (배치 G1, ADR 0017).

화면 구조는 2026-10 기준 threads.com 한국어 UI에서 확인했다. 화면이 바뀌면 여기 JS만 고친다.
게시물 ID(external_id)는 URL의 `/post/<code>` 부분이다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from nsm.adapters.platforms.aside_common import (
    JS_ASSERT_HANDLE,
    AsideAdapterBase,
    PlatformError,
    RateLimitedError,
    check_upload_paths,
    norm_handle,
    post_probe,
    reply_probe,
)
from nsm.domain.models import (
    AccountMetrics,
    Comment,
    HealthStatus,
    PostMetrics,
    PublishResult,
    RenderedPost,
)
from nsm.ports import AdapterError

log = logging.getLogger(__name__)

BASE = "https://www.threads.com"
COMMENT_BATCH = 4  # repl 한 번에 읽을 글 수(글당 약 10초)
# 게시 전 멱등 확인 범위(C43에서 6시간 → 72시간). 재시도 간격 합은 15분 + 1시간 + 4시간 = 5시간 15분이지만,
# 게시 잡은 게시 시간 안에서만 돌아 재시도가 밤을 넘기고(예: 11:30 실패 → 12:45 재시도가 18:00에), 절전·
# 요청 제한 쉬기(최대 8시간)·플랫폼 멈춤도 재시도를 민다. 6시간이면 첫 시도에 올라간 글을 못 찾고 다시 올린다.
# 같은 고유 문구(본문 앞 40자)의 다른 글이 사흘 안에 있을 일은 드물어 넓게 잡는다
IDEMPOTENCY_WINDOW = timedelta(hours=72)
FIND_AFTER_POST_MS = 90_000  # 게시 뒤 프로필에서 글을 찾는 최대 시간(3초 간격, C43: 45초 → 90초)

JS_HANDLE = (
    JS_ASSERT_HANDLE
    + r"""
const MAIN = '[aria-label="칼럼 본문"]';  // 가운데 칼럼(왼쪽 메뉴의 같은 이름 아이콘과 구분)
// 왼쪽 메뉴의 프로필 링크에서 내 핸들을 읽는다. 아이콘 이름은 svg의 aria-label(2026-10) 또는
// svg 안 <title> 글(2026-11-05~)로 붙는다. 둘 다 없으면 설정 핸들 그대로의 메뉴 링크를 쓴다(C38, ADR 0063)
async function readHandle(pg) {
  await nsmUntil(async () => (await pg.locator('a[href^="/@"]').count()) > 0, 20000);
  return await pg.evaluate((args) => {
    const NAMES = ['프로필', 'Profile'];
    const isProfile = (svg) => {
      if (NAMES.includes((svg.getAttribute('aria-label') || '').trim())) return true;
      const t = svg.querySelector('title');
      return !!t && NAMES.includes((t.textContent || '').trim());
    };
    for (const a of document.querySelectorAll('a[href^="/@"]')) {
      if (Array.from(a.querySelectorAll('svg')).some(isProfile)) {
        return a.getAttribute('href').slice(2).split('/')[0];
      }
    }
    if (!args.handle) return '';
    // 마지막 대체: 글 카드·가운데 칼럼 밖(왼쪽 메뉴)의 아이콘 링크. 글 작성자 링크는 내 계정이 아니어도 같은 모양이다
    for (const a of document.querySelectorAll('a[href="/@' + args.handle + '"]')) {
      if (a.closest('[data-pressable-container="true"]') || a.closest(args.main)) continue;
      if (a.closest('nav') || a.querySelector('svg')) return args.handle;
    }
    return '';
  }, { handle: ARGS.handle || '', main: MAIN });
}
"""
)

_HEALTH = (
    JS_HANDLE
    + r"""
// 홈 하나만 연다. 로그인이 안 보이면 그 화면 정보로 무엇이 막는지 나눈다(E5). 확인 창은 누르지 않는다
const pg = await nsmOpen('https://www.threads.com/');
await nsmCheckPage(pg);
const me = ((await readHandle(pg)) || '').toLowerCase();
if (!me) await nsmCheckPage(pg);  // 오류 화면이면 로그인 문제가 아니다(C37)
if (!me && nsmLeft() > 6000) return { me, page: await nsmEvidence({ noShot: !ARGS.shot }) };
return { me };
"""
)

_FIND_MINE = r"""
// 프로필 최신 글 중 고유 문구(probe)를 가진 내 글의 코드
// noNav: 방금 그 주소로 연 탭이면 다시 읽지 않는다. 읽기 시간은 마감 10초 전까지(최대 30초, C58)
async function findMine(pg, noNav) {
  if (!noNav) await pg.goto('https://www.threads.com/@' + ARGS.handle, { timeout: Math.max(1000, Math.min(30000, nsmLeft() - 10000)) });
  await sleep(2500);
  return await pg.evaluate((args) => {
    for (const a of document.querySelectorAll('a[href*="/post/"]')) {
      const href = a.getAttribute('href');
      if (!href.startsWith('/@' + args.handle + '/post/')) continue;
      const box = a.closest('[data-pressable-container="true"]') || a.parentElement;
      // 줄바꿈·연속 공백을 한 칸으로: probe는 본문 줄을 공백으로 이어 만든다
      if (!box || !(box.innerText || '').replace(/\s+/g, ' ').includes(args.probe)) continue;
      const t = box.querySelector('time');
      // 재시도 범위(약 6시간) 안에 올린 글만 같은 글로 본다. 오래된 비슷한 글과 헷갈리지 않게.
      if (args.since && t && Date.parse(t.getAttribute('datetime')) < Date.parse(args.since)) continue;
      return href.split('/post/')[1].split(/[/?]/)[0];
    }
    return '';
  }, { handle: ARGS.handle, probe: ARGS.probe, since: ARGS.since });
}
"""

# 게시 뒤 게시 탭에서 기다리기와 확인 탭(C58, ADR 0078). node 가짜 실행기로 따로 시험한다
_SETTLE = r"""
// 게시 탭의 상태 문구: 글 카드·대화상자 밖의 글만 본다(내 글 본문의 "게시 중" 같은 말에 걸리지 않게)
async function postStatusText(pg) {
  return await pg.evaluate(() => {
    let t = (document.body && document.body.innerText) || '';
    for (const el of document.querySelectorAll('[data-pressable-container="true"], [role="dialog"]')) {
      const s = el.innerText || '';
      if (s) t = t.split(s).join(' ');
    }
    return t;
  }).catch(() => '');
}
const NSM_POSTING = /게시 중|Posting/;
const NSM_POSTED = /게시됨|게시되었|Posted/;
// '게시 중'이 사라지거나 '게시됨'이 보일 때까지(최대 ms). 돌려주는 값: 본 '게시됨' 문구, 끝까지 '게시 중'이면
// 'posting', '게시 중'이 보였다 사라졌으면 'posting-gone', 아무 문구도 없었으면 ''. 실패로 만들지 않는다.
async function waitPostSettled(pg, ms) {
  let settle = '';
  let saw = false;
  await nsmUntil(async () => {
    const t = await postStatusText(pg);
    const done = t.match(NSM_POSTED);
    if (done) { settle = done[0]; return true; }
    if (NSM_POSTING.test(t)) { saw = true; settle = 'posting'; return false; }
    settle = saw ? 'posting-gone' : '';
    return true;
  }, ms, 1000);
  return settle;
}
// 확인 탭: 처음 부를 때 한 번 열고(연 것이 첫 읽기), 다음부터는 findMine의 goto로 다시 읽는다.
// 게시를 누른 뒤라 어떤 오류도 던지지 않고 '못 찾음'으로 본다(던지면 finally가 게시 탭을 닫는다).
function makeChecker(url) {
  let chk = null;
  return async () => {
    try {
      if (!chk) { chk = await nsmOpen(url); return await findMine(chk, true); }
      return await findMine(chk);
    } catch (e) { return ''; }
  };
}
"""

_PUBLISH = (
    JS_HANDLE
    + _FIND_MINE
    + _SETTLE
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle);
await nsmAssertHandle(pg, readHandle);
// 멱등: 지난 시도가 실제로는 올라갔다면(시간 초과 등) 다시 올리지 않고 그 글을 돌려준다
const before = ARGS.probe ? await findMine(pg) : '';
if (before) return { code: before, existing: true };
const files = await nsmFiles();
const comp = await nsmWait(pg.locator('[role="button"]').filter({ hasText: '새로운 소식을 공유해보세요' }));
await comp.click();
const dlg = pg.locator('[role="dialog"]');
const box = await nsmWait(dlg.locator('[contenteditable="true"]'));
await box.click();
await pg.keyboard.insertText(ARGS.text);
if (files.length) {
  await dlg.locator('input[type="file"]').first().setInputFiles(files);
  const ok = await nsmUntil(async () => (await dlg.locator('img[src^="blob:"], video').count()) >= files.length, 60000);
  if (!ok) throw new NsmError('ui', '이미지 미리보기가 나타나지 않음');
  // 미리보기가 생긴 직후에는 업로드 준비가 덜 끝났을 수 있다(C58). 잠깐 두고, '게시'가 활성일 때 누른다
  await sleep(1500);
}
// nsmClickText는 비활성(aria-disabled·disabled) 버튼을 건너뛴다. 활성이 될 때까지 다시 본다(C58)
if (!(await nsmUntil(() => nsmClickText(dlg, '게시'), 15000, 500))) throw new NsmError('ui', '게시 버튼 없음 또는 계속 비활성');
// 게시 버튼을 누른 뒤에는 던지지 않고 결과로 돌려준다. 글을 못 찾으면 Python이 "확인 실패"로 재시도하고,
// 재시도는 게시 전에 위의 findMine으로 같은 글을 먼저 찾아 중복을 막는다(C43, ADR 0020 개정 1).
// 증거는 게시 탭(pg)에서 찍는다. 탭은 스크립트가 끝나면 닫힌다.
const evidence = async () => (ARGS.probe && nsmLeft() > 6000 ? await nsmEvidence({ page: pg }).catch(() => null) : null);
const closed = await nsmUntil(async () => (await pg.locator('[role="dialog"] [contenteditable="true"]').count().catch(() => 1)) === 0, 90000);
if (!closed) return { code: '', posted: 'unknown', page: await evidence() };
// 스레드 웹은 '게시'를 누르면 창을 먼저 닫고 업로드·게시를 뒤에서 마친다. 이 탭을 바로 다른 주소로 옮기면
// 진행 중인 요청이 끊겨 글이 올라가지 않았다(2026-11-13~11-24 11회, incidents/0017, ADR 0078).
// 그래서 게시 탭은 끝까지 그 주소에 그대로 두고, 최소 5초 + "게시 중"이 사라지거나 "게시됨"이 보일 때까지
// (최대 15초) 기다린 뒤, 확인은 따로 연 탭에서 한다. 문구가 안 보여도 실패로 보지 않고 넘어간다.
await sleep(Math.min(5000, nsmLeft()));
let settle = await waitPostSettled(pg, 15000);
// 못 찾았을 때 증거(화면 5초 + 글 3초)를 찍을 시간을 남긴다. nsmUntil은 마감을 회차 앞에서만 보므로
// 한 회차(읽기 최대 15초 + 대기 2.5초 + 간격 3초)만큼 더 남긴다: 인스타그램과 같은 25초 여유.
const check = makeChecker('https://www.threads.com/@' + ARGS.handle);
const wait = Math.min(ARGS.find_ms, nsmLeft() - 25000);
let found = ARGS.probe && wait > 0 ? await nsmUntil(check, wait, 3000) : '';
if (found) return { code: found, posted: true, settle };
// 못 찾았는데 게시 탭에 아직 "게시 중"이 보이면 증거 시간만 남기고 더 기다린다. 스크립트가 끝나면 탭이
// 닫혀 업로드가 끊기기 때문이다. 다 끝나면 시간이 남을 때 한 번 더 찾는다.
if (nsmLeft() > 13000 && NSM_POSTING.test(await postStatusText(pg))) {
  settle = (await waitPostSettled(pg, nsmLeft() - 12000)) || settle;
  if (ARGS.probe && settle !== 'posting' && nsmLeft() > 30000) found = await check();
  if (found) return { code: found, posted: true, settle };
}
return { code: '', posted: true, settle, page: await evidence() };
"""
)

_DELETE = (
    JS_HANDLE
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + ARGS.code);
await nsmAssertHandle(pg, readHandle);
// 답글이면 위에 원글 맥락이 함께 보인다. 메뉴를 차례로 열어 "삭제"가 있는 것(내 글)을 고른다.
await nsmNeed(pg, () => nsmMarkIcon(pg, MAIN, '더 보기', 0), 30000, '글 메뉴(더 보기) 없음', 'ui');
let done = false;
for (let i = 0; i < 4 && !done; i++) {
  const more = await nsmMarkIcon(pg, MAIN, '더 보기', i);
  if (!more) break;
  await more.click();
  await sleep(800);
  done = await nsmClickText(pg, '삭제');
  if (!done) { await pg.keyboard.press('Escape'); await sleep(400); }
}
if (!done) throw new NsmError('ui', '삭제 메뉴 없음');
await sleep(800);
const dlg = pg.locator('[role="dialog"]');
if (!(await nsmClickText(dlg, '삭제'))) throw new NsmError('ui', '삭제 확인 버튼 없음');
await sleep(2000);
return { deleted: true };
"""
)


JS_NUM = r"""
function nsmNum(s) {
  s = String(s || '').replace(/,/g, '').trim();
  const m = s.match(/([0-9]+(?:\.[0-9]+)?)\s*(천|만|K|M)?/i);
  if (!m) return 0;
  const mult = { '천': 1e3, '만': 1e4, 'k': 1e3, 'm': 1e6 }[(m[2] || '').toLowerCase()] || 1;
  return Math.round(parseFloat(m[1]) * mult);
}
"""

_POST_METRICS = (
    JS_HANDLE
    + JS_NUM
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + ARGS.code);
await nsmAssertHandle(pg, readHandle);
// 글 페이지에서 처음 나오는 동작 버튼들이 본문 글의 것이다(답글들은 그 아래). 아이콘 이름(svg aria-label·<title>)
// → 가장 가까운 버튼 → 그 글을 evaluate 한 번 안에서 읽는다. 표시를 달고 locator로 다시 찾으면 그 사이 화면이
// 다시 그려져 표시가 사라질 수 있다(2026-11-10 03:23 "Selector [data-nsm-mark=...] not found", C43)
const readCounts = () => pg.evaluate(([main, labs]) => {
  const scopes = Array.from(document.querySelectorAll(main));
  const svgs = Array.from(new Set(scopes.flatMap(sc => Array.from(sc.querySelectorAll('svg')))));
  const out = {};
  for (const lab of labs) {
    // 표시 달기 도우미와 같은 순서: 이름이 맞는 svg 중 버튼 안에 있는 첫 것
    const b = svgs.filter(s => {
      const t = s.querySelector('title');
      return s.getAttribute('aria-label') === lab || (t && t.textContent === lab);
    }).map(s => s.closest('[role="button"], button, a')).find(Boolean);
    out[lab] = b ? (b.innerText || '').trim() : null;
  }
  return out;
}, [MAIN, ['좋아요', '답글', '리포스트', '공유하기']]);
const counts = await nsmNeed(pg, async () => {
  if (!(await pg.locator('svg title').count())) return null;
  const c = await readCounts();
  return c['좋아요'] === null ? null : c;
}, 30000, '게시물 본문이 30초 안에 열리지 않음');
await pg.goto('https://www.threads.com/insights/post/' + ARGS.code);
const txt = await nsmUntil(async () => {
  const t = await pg.evaluate(() => document.body.innerText);
  return /조회수\s*[0-9]/.test(t) ? t : '';
}, 20000);
const pick = (re) => { const m = (txt || '').match(re); return m ? nsmNum(m[1]) : null; };
return {
  likes: counts['좋아요'] === null ? null : nsmNum(counts['좋아요']),
  comments: counts['답글'] === null ? null : nsmNum(counts['답글']),
  shares: counts['리포스트'] === null ? null : nsmNum(counts['리포스트']),
  views: pick(/조회수\s*([0-9.,]+\s*(?:천|만)?)/),
  reach: pick(/조회한 사람\s*([0-9.,]+\s*(?:천|만)?)/),
};
"""
)

_ACCOUNT_METRICS = (
    JS_HANDLE
    + JS_NUM
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle);
await nsmAssertHandle(pg, readHandle);
const t = await nsmNeed(pg, async () => {
  const s = await pg.evaluate(() => Array.from(document.querySelectorAll('[role="button"], a')).map(b => (b.innerText || '').trim()).find(x => /^팔로워/.test(x)) || '');
  return s;
}, 20000, '프로필이 20초 안에 열리지 않음');
return { followers: t ? nsmNum(t.replace('팔로워', '')) : null };
"""
)


# 글 페이지의 답글 목록: 첫 컨테이너는 본문 글, 그다음부터 답글(각자 /post/<code>를 가진 글)
_COMMENTS = (
    JS_HANDLE
    + r"""
const out = [];
const failed = [];  // 본문이 안 열려 건너뛴 글(C37)
let firstErr = null;
for (const code of ARGS.codes) {
  const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + code);
  // 둘째 글부터 막혀도(429) 알아보게 글마다 페이지를 검사한다. 핸들은 처음 한 번만
  try {
    if (code === ARGS.codes[0]) await nsmAssertHandle(pg, readHandle); else await nsmCheckPage(pg);
    // 첫 컨테이너가 본문 글이다. 본문이 안 뜨면 빈 목록이 아니라 오류(2026-11-04: 13시간 "새 댓글 없음", C37).
    // 본문은 떴는데 컨테이너가 하나뿐이면 정말 댓글이 없는 글이다(빈 목록 정상)
    await nsmNeed(pg, async () => (await pg.locator(MAIN + ' [data-pressable-container="true"]').count()) > 0, 20000, '게시물 본문이 20초 안에 열리지 않음');
    await sleep(2000);
    const rows = await pg.evaluate((MAIN) => Array.from(document.querySelectorAll(MAIN + ' [data-pressable-container="true"]')).slice(1).map(c => {
      const post = Array.from(c.querySelectorAll('a[href*="/post/"]')).map(a => a.getAttribute('href')).find(h => /^\/@[^/]+\/post\/[^/?]+$/.test(h)) || '';
      const time = c.querySelector('time');
      const handle = post ? post.split('/')[1].slice(1) : '';
      const lines = (c.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      const ti = lines.findIndex(l => time && (l === time.innerText.trim()));
      const body = lines.slice(ti >= 0 ? ti + 1 : 2).filter(l => !/^[0-9.,]+(천|만)?$/.test(l)).join('\n');
      return { code: post ? post.split('/post/')[1] : '', handle, text: body, at: time ? time.getAttribute('datetime') : null };
    }), MAIN);
    for (const r of rows) if (r.code && r.at) out.push({ ...r, parent: code });
  } catch (e) {
    // 로그인·요청 제한·화면 변경은 그대로 던진다. 글이 안 열린 것(page)만 그 글을 건너뛴다
    if (!e || e.code !== 'page') throw e;
    failed.push(code);
    firstErr = firstErr || e;
    if (failed.length === ARGS.codes.length) throw firstErr;  // 모두 안 열림: 탭을 남겨 증거로
  }
  await closeTab(pg); __opened.splice(__opened.indexOf(pg), 1);
}
return { rows: out, failed };
"""
)

_REPLY = (
    JS_HANDLE
    + r"""
// 댓글 자체 페이지: 위에 내 글(답글 아이콘 0번), 그 아래 댓글(1번). 1번을 누르면 "답글" 창이 열린다.
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.author + '/post/' + ARGS.code);
await nsmAssertHandle(pg, readHandle);
const icon = await nsmNeed(pg, () => nsmMarkIcon(pg, MAIN, '답글', 1), 30000, '댓글의 답글 버튼 없음', 'ui');
await icon.click();
await nsmWait(pg.locator('[role="dialog"] [contenteditable="true"]'));
// 창에 인용된 원글이 그 댓글(작성자)인지 확인하고 쓴다
if ((await pg.locator('[role="dialog"] a[href="/@' + ARGS.author + '"]').count()) === 0) throw new NsmError('ui', '답글 창의 대상이 다름');
const dlg = pg.locator('[role="dialog"]');
const box = await nsmWait(dlg.locator('[contenteditable="true"]'));
await box.click();
await pg.keyboard.insertText(ARGS.text);
// 잠깐 비활성인 순간이 있어도 '버튼 없음'이 되지 않게 다시 본다(C58: nsmClickText가 비활성을 건너뜀)
if (!(await nsmUntil(() => nsmClickText(dlg, '게시'), 5000, 300))) throw new NsmError('ui', '답글 게시 버튼 없음');
// 여기부터는 답글이 나갔을 수 있으니 오류로 돌리지 않는다(사람이 다시 답하면 중복)
const closed = await nsmUntil(async () => (await pg.locator('[role="dialog"] [contenteditable="true"]').count()) === 0, 60000);
if (!closed) return { code: '' };
const mine = await nsmUntil(async () => {
  await pg.goto('https://www.threads.com/@' + ARGS.author + '/post/' + ARGS.code);
  await sleep(2500);
  return await pg.evaluate((args) => {
    for (const c of document.querySelectorAll('[data-pressable-container="true"]')) {
      const h = Array.from(c.querySelectorAll('a[href^="/@' + args.handle + '/post/"]')).map(a => a.getAttribute('href'))[0];
      if (h && (c.innerText || '').replace(/\s+/g, ' ').includes(args.probe)) return h.split('/post/')[1].split(/[/?]/)[0];
    }
    return '';
  }, { handle: ARGS.handle, probe: ARGS.probe });
}, 60000, 3000);
return { code: mine || '' };
"""
)

_HIDE = (
    JS_HANDLE
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + ARGS.parent);
await nsmAssertHandle(pg, readHandle);
await nsmNeed(pg, async () => (await pg.locator(MAIN + ' a[href$="/post/' + ARGS.code + '"]').count()) > 0, 30000, '숨길 댓글이 30초 안에 보이지 않음');
const key = 'h' + Math.random().toString(36).slice(2);
const ok = await pg.evaluate(([code, key]) => {
  const a = document.querySelector('a[href$="/post/' + code + '"]');
  const c = a && a.closest('[data-pressable-container="true"]');
  if (!c) return false;
  const s = Array.from(c.querySelectorAll('svg')).find(s => { const t = s.querySelector('title'); return s.getAttribute('aria-label') === '더 보기' || (t && t.textContent === '더 보기'); });
  const b = s && s.closest('[role="button"], button');
  if (!b) return false;
  b.setAttribute('data-nsm-mark', key);
  return true;
}, [ARGS.code, key]);
if (!ok) throw new NsmError('ui', '답글 메뉴(더 보기) 없음');
await pg.locator('[data-nsm-mark="' + key + '"]').click();
await sleep(800);
if (!(await nsmClickText(pg, '모두에게 숨기기'))) throw new NsmError('ui', '"모두에게 숨기기" 메뉴 없음');
await sleep(1200);
const dlg = pg.locator('[role="dialog"]');
if ((await dlg.count()) > 0) {
  if (!(await nsmClickText(dlg, '숨기기'))) throw new NsmError('ui', '숨기기 확인 버튼 없음');
  await sleep(1200);
}
// 숨긴 답글은 목록에서 빠진다. 그대로 보이면 숨기지 못한 것이다.
await pg.reload(); await sleep(2500);
if ((await pg.locator('a[href$="/post/' + ARGS.code + '"]').count()) > 0) throw new NsmError('ui', '숨긴 뒤에도 댓글이 보임');
return { hidden: true };
"""
)


def _split_comment_id(external_id: str) -> tuple[str, str]:
    """댓글 ID는 "작성자:코드". 답글도 작성자 주소 아래의 글이라 둘 다 있어야 연다.

    "/"는 쓰지 않는다: 대시보드가 "플랫폼:ID"를 URL 경로에 넣는다(/comments/{key}/reply).
    """
    author, _, code = external_id.partition(":")
    if not author or not code:
        raise PlatformError(f"스레드 댓글 ID 형식이 아님: {external_id}", "config_or_bug")
    return author, code


_probe = post_probe  # 규칙은 aside_common.post_probe 한 곳(인스타와 같음)


class ThreadsAdapter(AsideAdapterBase):
    platform = "threads"

    def health(self) -> HealthStatus:
        return self._health(_HEALTH, "스레드")

    def publish(self, post: RenderedPost) -> PublishResult:
        self._need("publish")
        images = check_upload_paths(post.images, self.media_dir)
        since = (self._now() - IDEMPOTENCY_WINDOW).isoformat()
        probe = _probe(post.text)
        args = {"text": post.text, "probe": probe, "since": since, "find_ms": FIND_AFTER_POST_MS}
        data = self._run(_PUBLISH, args, images) or {}
        code = data.get("code", "")
        if data.get("settle"):
            # 게시 뒤 게시 탭에 보인 문구(C58). 스레드의 실제 문구를 운영 로그로 확인하려고 남긴다
            log.info("스레드: 게시 뒤 화면 문구 %r", data.get("settle"))
        if data.get("existing"):
            log.warning("스레드: 지난 시도에서 이미 올라간 글을 찾아 다시 올리지 않음(%s)", code)
        if not code:
            if probe:
                # 프로필에서 글을 못 찾음 = 실제로 안 올라간 경우가 많았다(incidents/0015). 재시도로 돌리고,
                # 재시도는 게시 전 멱등 확인(findMine)이 이미 올라간 글을 찾아 중복을 막는다(C43)
                raise self._unverified(data)
            # 짧은 글(고유 문구 없음)은 다시 올리면 중복을 막을 수 없어 예전처럼 성공으로 기록한다.
            # external_id가 비면 댓글·지표 수집에서 빠진다.
            log.warning(
                "스레드: 게시는 됐지만 글 주소를 찾지 못함, 짧은 글이라 확인 생략(%s)",
                post.content_id,
            )
            return PublishResult(external_id="", url=f"{BASE}/@{self.handle}")
        return PublishResult(external_id=code, url=f"{BASE}/@{self.handle}/post/{code}")

    def delete_post(self, external_id: str) -> None:
        """포트에 없는 보조 동작: 계약 테스트가 남긴 글을 지운다."""
        self._run(_DELETE, {"code": external_id})

    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]:
        """내 글에 달린 남의 답글. 첫 화면에 보이는 답글까지만 읽는다(스크롤 더 보기는 안 함)."""
        self._need("fetch_comments")
        if not post_external_ids:
            return []
        rows: list[dict] = []
        errors: list[Exception] = []
        skipped: list[str] = []
        read = 0  # 읽은 묶음 수. 댓글이 0개여도 읽은 것이다(C37: "0개"와 "못 읽음"을 가른다)
        ids = list(post_external_ids)
        # 한 번의 repl(120초)에 글 몇 개만: 글이 쌓여도 제한에 걸려 통째로 실패하지 않게
        for i in range(0, len(ids), COMMENT_BATCH):
            try:
                data = self._run(_COMMENTS, {"codes": ids[i : i + COMMENT_BATCH]}) or []
                if isinstance(data, dict):  # {rows, failed}: 본문이 안 열린 글은 건너뜀(C37)
                    rows += data.get("rows") or []
                    skipped += list(data.get("failed") or [])
                else:
                    rows += data
                read += 1
            except RateLimitedError:
                raise  # 남은 묶음도 막힌다. 읽은 것도 버려 since를 앞당기지 않는다(다음에 다시 읽음)
            except AdapterError as e:
                errors.append(e)
        if errors and not read:
            raise errors[0]
        if skipped:
            log.warning(
                "%s 댓글: 본문이 열리지 않은 글 %d개를 건너뜀: %s",
                self.platform,
                len(skipped),
                skipped[:4],
            )
        if errors:
            # 일부 글을 못 읽으면 그 글의 새 댓글을 다음 실행에서 놓칠 수 있다(since가 앞당겨짐)
            log.warning(
                "%s 댓글: 글 묶음 %d개를 읽지 못함: %s", self.platform, len(errors), errors[0]
            )
        out: list[Comment] = []
        for r in rows:
            if norm_handle(r["handle"]) == self.handle:
                continue  # 내 답글
            at = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
            if since is not None and at <= since:
                continue
            out.append(
                Comment(
                    platform="threads",
                    external_id=f"{r['handle']}:{r['code']}",
                    post_external_id=r["parent"],
                    author_id=r["handle"],
                    author_name=r["handle"],
                    text=r["text"],
                    created_at=at,
                )
            )
        return out

    def reply(self, comment: Comment, text: str) -> str:
        self._need("reply")
        author, code = _split_comment_id(comment.external_id)
        d = self._run(
            _REPLY,
            {
                "author": author,
                "code": code,
                "parent": comment.post_external_id,
                "text": text,
                "probe": reply_probe(text),
            },
        )
        if not d.get("code"):
            log.warning("스레드: 답글은 보냈지만 확인하지 못함(%s)", comment.external_id)
        return d.get("code", "")

    def hide_comment(self, comment: Comment) -> None:
        self._need("hide_comment")
        _author, code = _split_comment_id(comment.external_id)
        self._run(_HIDE, {"parent": comment.post_external_id, "code": code})

    def read_post_metrics(self, post_external_id: str) -> PostMetrics:
        self._need("post_metrics")
        d = self._run(_POST_METRICS, {"code": post_external_id}) or {}
        return PostMetrics(
            platform="threads",
            post_external_id=post_external_id,
            captured_at=self._now(),
            views=d.get("views"),
            reach=d.get("reach"),
            likes=d.get("likes"),
            comments=d.get("comments"),
            shares=d.get("shares"),
        )

    def read_account_metrics(self) -> AccountMetrics:
        self._need("account_metrics")
        d = self._run(_ACCOUNT_METRICS, {}) or {}
        return AccountMetrics(
            platform="threads", captured_at=self._now(), followers=d.get("followers")
        )
