"""인스타그램 실계정 어댑터: Aside 브라우저를 `aside repl`로 조작한다 (배치 G2, ADR 0017·0020).

화면 구조는 2026-10 기준 instagram.com 한국어 UI에서 확인했다. 화면이 바뀌면 여기 JS만 고친다.

- 게시: 만들기 → 게시물 → 파일 → 자르기 **4:5** → 다음(편집) → 다음(캡션) → "Threads에 공유" 끄기 → 공유하기.
  올린 뒤 글을 열어 이미지 비율이 4:5(0.8)인지 확인한다. 1:1로 잘리면 위쪽 "가상인물" 배지가 사라진다(1일차 검증).
- 댓글 숨기기는 웹에 없다(ADR 0018) → capability에서 빠져 있고, 스팸은 사람 검토로 간다.
- 게시물 ID(external_id)는 URL의 `/p/<code>/` 부분이다.
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

BASE = "https://www.instagram.com"
COMMENT_BATCH = 4
IDEMPOTENCY_WINDOW = timedelta(hours=6)
CARD_RATIO = 0.8  # 4:5
RATIO_TOLERANCE = 0.03

JS_IG = (
    JS_ASSERT_HANDLE
    + r"""
async function readHandle(pg) {
  await nsmUntil(async () => (await pg.locator('img[alt$="님의 프로필 사진"]').count()) > 0, 20000);
  return await pg.evaluate(() => {
    // 왼쪽 메뉴의 "프로필" 링크(내 프로필 사진)가 첫 번째로 나온다
    for (const a of document.querySelectorAll('a[href]')) {
      const img = a.querySelector('img[alt$="님의 프로필 사진"]');
      const href = a.getAttribute('href') || '';
      if (img && /^\/[^/]+\/$/.test(href)) return href.slice(1, -1);
    }
    return '';
  });
}
function nsmNum(s) {
  s = String(s || '').replace(/,/g, '').trim();
  const m = s.match(/([0-9]+(?:\.[0-9]+)?)\s*(천|만|K|M)?/i);
  if (!m) return 0;
  const mult = { '천': 1e3, '만': 1e4, 'k': 1e3, 'm': 1e6 }[(m[2] || '').toLowerCase()] || 1;
  return Math.round(parseFloat(m[1]) * mult);
}
// 대화상자 단계: 스냅샷의 ref로 버튼을 누른다(같은 이름 버튼이 숨은 이전 단계에도 있어 DOM 검색은 빗나감)
async function dialogName(pg) {
  const s = await snapshot(pg, { selector: '[role="dialog"]', interactive: true });
  const k = s.tree.indexOf('dialog "');
  return k < 0 ? '' : s.tree.slice(k + 8).split('"')[0];
}
async function dialogButton(pg, label) {
  const s = await snapshot(pg, { selector: '[role="dialog"]', interactive: true });
  const key = 'button "' + label + '" [ref=';
  const k = s.tree.indexOf(key);
  return k < 0 ? null : pg.locator(s.tree.slice(k + key.length).split(']')[0]);
}
// 처리 중인 버튼은 비활성이다(누르면 "Element is disabled"). 이미지 처리 중이므로 누르지 않고 기다린다.
const nsmDisabled = async (b) => await b.evaluate(e => e.disabled === true || e.getAttribute('aria-disabled') === 'true');
async function clickDialogButton(pg, label) {
  const b = await dialogButton(pg, label);
  if (!b) return false;
  if (await nsmDisabled(b)) return 'busy';
  try { await b.click(); } catch (e) { if (/disabled/i.test(String((e && e.message) || e))) return 'busy'; throw e; }
  return true;
}
// 다음 단계로: 화면이 덜 준비됐을 때 누른 "다음"은 무시되거나(2026-10-14 실패) 버튼이 처리 중(비활성)이 된다.
// 처리 중이면 풀릴 때까지 기다리고, 넘어가지 않았으면 다시 누른다. 최대 60초.
async function nextStep(pg, from) {
  const end = Date.now() + Math.min(60000, nsmLeft() - 30000);
  while (Date.now() < end) {
    await nsmDismissPrompts(pg);
    // 비활성('busy')이면 누르지 않고 짧게 기다렸다 다시 본다
    const r = await clickDialogButton(pg, '다음');
    const moved = await nsmUntil(async () => (await dialogName(pg)) !== from, r === true ? 8000 : 2000, 800);
    if (moved) return true;
  }
  return false;
}
// 글 페이지 정보: 캡션, 시각, 이미지 비율
async function postInfo(pg, code) {
  await pg.goto('https://www.instagram.com/p/' + code + '/');
  await nsmUntil(async () => (await pg.locator('article img, main img').count()) > 0, 20000);
  await sleep(1500);
  return await pg.evaluate(([code, handle]) => {
    const imgs = Array.from(document.querySelectorAll('main img')).filter(i => i.naturalWidth > 300 && !(i.alt || '').endsWith('님의 프로필 사진'));
    const img = imgs[0];
    // 캡션 칸: 댓글 링크(/c/) 밖의 time 중, 같은 칸에 내 프로필 링크가 있는 것
    let capText = '', capAt = null;
    for (const t of document.querySelectorAll('main time')) {
      if (t.closest('a[href*="/c/"]')) continue;
      let box = t.parentElement;
      for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
        if (box.querySelector('a[href*="/c/"]')) break;
        // 핸들·시각 두 줄뿐인 머리 칸이 아니라 본문까지 품은 칸(3줄 이상)
        const lines = (box.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        if (box.querySelector('a[href="/' + handle + '/"]') && lines.length >= 3) {
          capText = box.innerText.replace(/\s+/g, ' ');
          capAt = t.getAttribute('datetime');
          break;
        }
      }
      if (capText) break;
    }
    return { code, text: capText, at: capAt, ratio: img ? img.naturalWidth / img.naturalHeight : null };
  }, [code, ARGS.handle]);
}
async function latestCodes(pg, n) {
  await pg.goto('https://www.instagram.com/' + ARGS.handle + '/');
  await nsmUntil(async () => (await pg.locator('a[href*="/p/"]').count()) > 0, 15000);
  return await pg.evaluate((n) => {
    const out = [];
    for (const a of document.querySelectorAll('a[href*="/p/"]')) {
      const m = (a.getAttribute('href') || '').match(/\/p\/([^/?]+)/);
      if (m && !out.includes(m[1])) out.push(m[1]);
      if (out.length >= n) break;
    }
    return out;
  }, n);
}
// 최근 글 몇 개 중 고유 문구가 있고 since 이후에 올린 글
async function findMine(pg, n) {
  if (!ARGS.probe) return null;
  for (const code of await latestCodes(pg, n)) {
    const info = await postInfo(pg, code);
    if (info.text.includes(ARGS.probe) && (!ARGS.since || !info.at || Date.parse(info.at) >= Date.parse(ARGS.since))) return info;
  }
  return null;
}
"""
)

_HEALTH = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/');
await nsmCheckPage(pg);
return { me: ((await readHandle(pg)) || '').toLowerCase() };
"""
)

_PUBLISH = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/');
await nsmAssertHandle(pg, readHandle);
// 멱등: 지난 시도가 실제로는 올라갔다면 다시 올리지 않는다
const before = await findMine(pg, 2);
if (before) return { code: before.code, ratio: before.ratio, existing: true };
await pg.goto('https://www.instagram.com/');
const files = await nsmFiles();
if (!files.length) throw new NsmError('script', '인스타그램 게시에는 이미지가 필요함');
const np = await nsmUntil(() => nsmMarkIcon(pg, null, '새로운 게시물', 0), 20000);
if (!np) throw new NsmError('script', '"새로운 게시물" 메뉴 없음');
await np.click();
const postItem = await nsmUntil(() => nsmMarkIcon(pg, null, '게시물', 0), 10000);
if (postItem) await postItem.click();
// "알림 설정" 안내가 업로드 창 앞에 겹치면 닫는다(2026-10-18부터 뜸)
await nsmDismissPrompts(pg);
const fi = pg.locator('[role="dialog"] input[type="file"]').first();
await fi.waitFor({ state: 'attached', timeout: Math.max(1000, Math.min(20000, nsmLeft())) });
await fi.setInputFiles(files);
// 자르기: 반드시 4:5. 안내 창이 뒤늦게 떠도 닫고 찾는다
const crop = await nsmUntil(async () => { await nsmDismissPrompts(pg); return nsmMarkIcon(pg, '[role="dialog"]', '자르기 선택', 0); }, 30000);
if (!crop) throw new NsmError('script', '자르기 단계가 나오지 않음');
await crop.click();
await sleep(800);
if (!(await nsmClickText(pg.locator('[role="dialog"]'), '4:5'))) throw new NsmError('script', '4:5 자르기 버튼 없음');
await sleep(800);
if (!(await nextStep(pg, '자르기'))) throw new NsmError('script', '자르기 다음 단계로 못 감');
await sleep(2500);
if (!(await nextStep(pg, '편집'))) throw new NsmError('script', '편집 다음 단계로 못 감');
const cap = await nsmWait(pg.locator('[role="dialog"] [contenteditable="true"]'), 20000);
await cap.click();
await pg.keyboard.insertText(ARGS.text);
// "Threads에 공유" 스위치는 끈다(스레드는 따로 올린다. 켜져 있으면 두 번 올라감)
const threadsSwitch = async (turnOff) => await pg.evaluate((turnOff) => {
  const isOn = (sw) => sw.getAttribute('aria-checked') === 'true' || sw.checked === true;
  const out = [];
  for (const sw of document.querySelectorAll('[role="dialog"] [role="switch"]')) {
    // 스위치에서 위로 올라가며 "Threads" 문구가 처음 나오는 칸 = 그 스위치의 행(다른 스위치를 품으면 멈춤)
    let row = sw.parentElement, label = '';
    for (let i = 0; i < 6 && row; i++, row = row.parentElement) {
      if (row.querySelectorAll('[role="switch"]').length > 1) break;
      if (/Threads/.test(row.innerText || '')) { label = row.innerText; break; }
    }
    if (!label) continue;
    if (turnOff && isOn(sw)) sw.click();
    out.push(isOn(sw));
  }
  return out;
}, turnOff);
await threadsSwitch(true);
await sleep(600);
const thrState = await threadsSwitch(false);
if (thrState.some(Boolean)) throw new NsmError('script', '"Threads에 공유" 스위치를 끄지 못함(중복 게시 위험)');
// 공유하기도 처리 중이면 비활성이다. 실제로 누를 때까지 기다린다('busy'는 누른 게 아님).
const shareClicked = await nsmUntil(async () => (await clickDialogButton(pg, '공유하기')) === true, 20000, 1000);
if (!shareClicked) throw new NsmError('script', '공유하기 버튼 없음 또는 계속 비활성');
// 여기부터는 올라갔을 수 있으니 어떤 오류도 밖으로 내지 않는다(오류 → 재시도 → 중복 게시)
try {
  const shared = await nsmUntil(async () => /공유되었습니다|게시물이 공유/.test(await pg.evaluate(() => document.body.innerText)), 60000, 1500);
  if (!shared) return { code: '', posted: 'unknown' };
  await pg.keyboard.press('Escape');
  // 글 하나를 여는 데 약 10초. 120초 강제 종료에 걸리지 않을 만큼 남았을 때만 주소를 찾는다.
  const mine = nsmLeft() > 25000 ? await findMine(pg, 2) : null;
  return { code: mine ? mine.code : '', ratio: mine ? mine.ratio : null, posted: true };
} catch (e) {
  return { code: '', posted: 'unknown', note: String((e && e.message) || e).slice(0, 200) };
}
"""
)

_DELETE = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/p/' + ARGS.code + '/');
await nsmAssertHandle(pg, readHandle);
const more = await nsmUntil(() => nsmMarkIcon(pg, 'main', '옵션 더 보기', 0), 20000);
if (!more) throw new NsmError('script', '글 메뉴(옵션 더 보기) 없음');
await more.click();
await sleep(1000);
if (!(await nsmClickText(pg.locator('[role="dialog"]'), '삭제'))) throw new NsmError('script', '삭제 메뉴 없음');
await sleep(1000);
if (!(await nsmClickText(pg.locator('[role="dialog"]'), '삭제'))) throw new NsmError('script', '삭제 확인 버튼 없음');
await sleep(2500);
return { deleted: true };
"""
)

# 글 페이지의 댓글: 각 댓글은 작성자 링크 + 본문 + time. 캡션(내 글 본문)도 같은 모양이라 내 핸들은 뺀다.
_COMMENTS = (
    JS_IG
    + r"""
const out = [];
let checked = false;
for (const code of ARGS.codes) {
  const pg = await nsmOpen('https://www.instagram.com/p/' + code + '/');
  // 둘째 글부터 막혀도(429) 알아보게 글마다 페이지를 검사한다. 핸들은 처음 한 번만
  if (!checked) { await nsmAssertHandle(pg, readHandle); checked = true; } else { await nsmCheckPage(pg); }
  await nsmUntil(async () => (await pg.locator('main time').count()) > 0, 20000);
  await sleep(2000);
  // 댓글의 시각은 /p/<글>/c/<댓글ID>/ 링크 안에 있다. 거기서 위로 올라가 작성자 링크가 있는 칸을 찾는다.
  const rows = await pg.evaluate(() => {
    const res = [];
    for (const a of document.querySelectorAll('main a[href*="/c/"]')) {
      const t = a.querySelector('time');
      const cid = ((a.getAttribute('href') || '').match(/\/c\/([^/?]+)/) || [])[1];
      if (!t || !cid) continue;
      let box = a.parentElement, author = null;
      for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
        author = Array.from(box.querySelectorAll('a[href]')).find(x => /^\/[^/]+\/$/.test(x.getAttribute('href')) && (x.innerText || '').trim());
        if (author) break;
      }
      if (!author) continue;
      const handle = author.getAttribute('href').slice(1, -1);
      const tt = (t.innerText || '').trim();
      const bodyOf = (el) => {
        const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        const ti = lines.indexOf(tt);
        return lines.slice(ti >= 0 ? ti + 1 : 1).filter(l => !/^(답글 달기|좋아요 [0-9]+개|번역 보기|숨겨진 답글 보기.*|답글 보기.*)$/.test(l)).join('\n');
      };
      // 본문이 보일 때까지 더 올라가되, 다른 댓글까지 품는 칸(댓글 링크 2개 이상)은 넘지 않는다
      let body = bodyOf(box);
      for (let up = box.parentElement, i = 0; !body && up && i < 4; up = up.parentElement, i++) {
        if (up.querySelectorAll('a[href*="/c/"]').length > 1) break;
        body = bodyOf(up);
      }
      res.push({ handle, cid, text: body, at: t.getAttribute('datetime') });
    }
    return res;
  });
  for (const r of rows) if (r.at && r.handle) out.push({ ...r, parent: code });
  await closeTab(pg); __opened.splice(__opened.indexOf(pg), 1);
}
return out;
"""
)

_REPLY = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/p/' + ARGS.parent + '/');
await nsmAssertHandle(pg, readHandle);
await nsmUntil(async () => (await pg.locator('main time').count()) > 0, 20000);
await sleep(1500);
// 그 댓글(/c/<댓글ID>/ 링크가 있는 칸)의 "답글 달기"를 누른다
const key = 'q' + Math.random().toString(36).slice(2);
const ok = await pg.evaluate(([author, cid, key]) => {
  const a = document.querySelector('main a[href*="/c/' + cid + '/"]');
  let box = a && a.parentElement;
  for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
    if (!box.querySelector('a[href="/' + author + '/"]')) continue;
    const b = Array.from(box.querySelectorAll('[role="button"], button')).find(e => (e.innerText || '').trim() === '답글 달기');
    if (b) { b.setAttribute('data-nsm-mark', key); return true; }
  }
  return false;
}, [ARGS.author, ARGS.cid, key]);
if (!ok) throw new NsmError('script', '댓글의 "답글 달기" 없음');
await pg.locator('[data-nsm-mark="' + key + '"]').click();
// "답글 달기"를 누르면 입력창에 "@작성자 "가 채워지고 초점이 거기 간다. 그 상태가 "이 댓글의 답글"이라는 표시다.
// 여기서 입력창을 다시 클릭하면 인스타그램이 답글 대상을 잊어 새 댓글로 올라간다(2026-10-19 실험으로 확인).
// 그래서 클릭하지 않고, 채워진 뒤 그대로 이어서 쓴다.
const box = await nsmWait(pg.locator('main textarea, main [contenteditable="true"]'), 15000);
const readBox = async () => await box.evaluate(e => e.value !== undefined ? e.value : (e.innerText || ''));
const prefilled = await nsmUntil(async () => (await readBox()).startsWith('@' + ARGS.author), 8000, 300);
if (!prefilled) throw new NsmError('script', '답글 입력창에 "@' + ARGS.author + '"가 채워지지 않음');
const focused = await box.evaluate(e => document.activeElement === e);
if (!focused) await box.evaluate(e => e.focus());  // 마우스 클릭이 아니라 focus()만(답글 대상 유지)
await pg.keyboard.insertText(ARGS.text);
await sleep(500);
// 전송은 Enter로(C25). "게시"를 마우스로 누르면 그 순간 입력창이 초점을 잃고(blur), 인스타그램이 답글 대상을
// 지우는 것과 경쟁해 가끔 새 댓글로 올라갔다(2026-10-19 21:05, 같은 코드로 다른 때는 답글로 달림).
// Enter는 입력창 안에서 보내므로 초점이 옮겨 가지 않는다. Enter가 안 먹으면(글이 남아 있으면) "게시"를 누른다.
await pg.keyboard.press('Enter');
const posted = await nsmUntil(async () => (await readBox()) === '', 6000, 300);
if (!posted) {
  const sent = await nsmClickText(pg.locator('main'), '게시');
  if (!sent) throw new NsmError('script', '답글 게시 버튼 없음');
}
// 여기부터는 답글이 나갔을 수 있으니 오류로 돌리지 않는다
// 답글은 보통 "답글 N개 모두 보기"로 접혀 보인다. 내 문구가 보이거나 그 댓글 칸에 답글 수가 생기면 확인된 것으로 본다.
const seen = await nsmUntil(async () => await pg.evaluate((args) => {
  const main = document.querySelector('main');
  if ((main.innerText || '').includes(args.probe)) return true;
  const a = main.querySelector('a[href*="/c/' + args.cid + '/"]');
  let box = a && a.parentElement;
  for (let i = 0; i < 10 && box; i++, box = box.parentElement) {
    if (box.querySelectorAll('a[href*="/c/"]').length > 1) break;
    if (/답글 [0-9]+개/.test(box.innerText || '')) return true;
  }
  return false;
}, { probe: ARGS.probe, cid: ARGS.cid }), 30000, 2000);
return { ok: !!seen };
"""
)

_POST_METRICS = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/p/' + ARGS.code + '/');
await nsmAssertHandle(pg, readHandle);
await nsmUntil(async () => (await pg.locator('main time').count()) > 0, 20000);
await sleep(1500);
const comments = await pg.evaluate(() => document.querySelectorAll('main a[href*="/c/"] time').length);
// 인사이트(프로 계정)는 "숫자 이름" 순서로 보인다: "0 조회수 … 0 좋아요 0 댓글 0 저장 0 공유"
const k = 'i' + Math.random().toString(36).slice(2);
const has = await pg.evaluate((k) => {
  const e = Array.from(document.querySelectorAll('main a, main [role="button"], main div')).find(x => (x.innerText || '').trim() === '인사이트 보기');
  if (e) e.setAttribute('data-nsm-mark', k);
  return !!e;
}, k);
let t2 = '';
if (has) {
  await pg.locator('[data-nsm-mark="' + k + '"]').click();
  // 인사이트 화면(대화상자면 그 안, 페이지면 main)의 글만 읽는다. 캡션·댓글 문장("저장해 두세요")과 섞이지 않게.
  t2 = await nsmUntil(async () => {
    const t = await pg.evaluate(() => {
      const d = Array.from(document.querySelectorAll('[role="dialog"]')).find(x => /조회수/.test(x.innerText || ''));
      const el = d || (location.pathname.startsWith('/insights/') ? document.querySelector('main') || document.body : null);
      return el ? el.innerText.replace(/\s+/g, ' ') : '';
    });
    return /[0-9]\s*조회수/.test(t) ? t : '';
  }, 20000) || '';
}
const num = (re) => { const m = t2.match(re); return m ? nsmNum(m[1]) : null; };
return {
  // 숫자로 시작하고, 이름 바로 뒤가 공백·끝이어야 한다("공유하기", "댓글 달기" 제외)
  views: num(/([0-9][0-9.,]*\s*(?:천|만)?)\s*조회수(?=\s|$)/),
  reach: num(/조회한 사람\s*([0-9][0-9.,]*\s*(?:천|만)?)(?=\s|$)/),
  likes: num(/([0-9][0-9.,]*\s*(?:천|만)?)\s*좋아요(?=\s|$)/),
  comments: num(/([0-9][0-9.,]*\s*(?:천|만)?)\s*댓글(?=\s|$)/) ?? comments,
  saves: num(/([0-9][0-9.,]*\s*(?:천|만)?)\s*저장(?=\s|$)/),
  shares: num(/([0-9][0-9.,]*\s*(?:천|만)?)\s*공유(?=\s|$)/),
};
"""
)

_ACCOUNT_METRICS = (
    JS_IG
    + r"""
const pg = await nsmOpen('https://www.instagram.com/' + ARGS.handle + '/');
await nsmAssertHandle(pg, readHandle);
const t = await nsmUntil(async () => {
  const s = await pg.evaluate(() => (document.querySelector('header') || document.body).innerText.replace(/\s+/g, ' '));
  return /팔로워/.test(s) ? s : '';
}, 20000);
const m = (t || '').match(/팔로워\s*([0-9.,]+\s*(?:천|만)?)/);
return { followers: m ? nsmNum(m[1]) : null };
"""
)


def _probe(text: str) -> str:
    """이 글을 알아볼 고유 문구: 표기 줄·해시태그 줄을 뺀 본문 앞 40자(8자 미만이면 빈 문자열)."""
    lines = [ln.strip() for ln in text.splitlines()[1:]]
    body = " ".join(ln for ln in lines if ln and not ln.startswith("#"))
    return body[:40] if len(body) >= 8 else ""


def _split_comment_id(external_id: str) -> tuple[str, str]:
    """댓글 ID는 "작성자:댓글ID"(글 페이지의 /c/<댓글ID>/ 링크). "/"는 대시보드 경로를 깨므로 쓰지 않는다."""
    author, _, cid = external_id.partition(":")
    if not author or not cid:
        raise PlatformError(f"인스타그램 댓글 ID 형식이 아님: {external_id}", "config_or_bug")
    return author, cid


class InstagramAdapter(AsideAdapterBase):
    platform = "instagram"

    def health(self) -> HealthStatus:
        me = (self._call(_HEALTH, {}, probe=True) or {}).get("me", "")
        if not me:
            return HealthStatus(logged_in=False, detail="인스타그램 로그인 필요(Aside 브라우저)")
        if self.handle and me != self.handle:
            return HealthStatus(logged_in=False, detail=f"로그인 계정 @{me} ≠ 설정 @{self.handle}")
        return HealthStatus(logged_in=True, detail=f"@{me}")

    def publish(self, post: RenderedPost) -> PublishResult:
        self._need("publish")
        if not post.images:
            raise PlatformError("인스타그램 게시에는 이미지가 필요합니다", "config_or_bug")
        images = check_upload_paths(post.images, self.media_dir)
        since = (self._now() - IDEMPOTENCY_WINDOW).isoformat()
        args = {"text": post.text, "probe": _probe(post.text), "since": since}
        data = self._run(_PUBLISH, args, images) or {}
        code = data.get("code", "")
        if data.get("existing"):
            log.warning(
                "인스타그램: 지난 시도에서 이미 올라간 글을 찾아 다시 올리지 않음(%s)", code
            )
        if not code:
            log.warning("인스타그램: 게시는 됐지만 글 주소를 찾지 못함(%s)", post.content_id)
            return PublishResult(external_id="", url=f"{BASE}/{self.handle}/")
        ratio = data.get("ratio")
        if ratio is not None and abs(float(ratio) - CARD_RATIO) > RATIO_TOLERANCE:
            # 표기 배지가 잘렸을 수 있다. 글은 이미 올라갔으므로 기록은 하되 크게 알린다.
            log.error(
                "인스타그램: 카드 비율이 4:5가 아님(%.3f). 가상인물 배지가 잘렸는지 확인 필요: %s",
                ratio,
                code,
            )
        return PublishResult(external_id=code, url=f"{BASE}/p/{code}/")

    def delete_post(self, external_id: str) -> None:
        """포트에 없는 보조 동작: 계약 테스트가 남긴 글을 지운다."""
        self._run(_DELETE, {"code": external_id})

    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]:
        """내 글에 달린 남의 댓글. 글 화면에 처음 보이는 댓글까지만 읽는다."""
        self._need("fetch_comments")
        rows: list[dict] = []
        errors: list[Exception] = []
        ids = list(post_external_ids)
        for i in range(0, len(ids), COMMENT_BATCH):
            try:
                rows += self._run(_COMMENTS, {"codes": ids[i : i + COMMENT_BATCH]}) or []
            except RateLimitedError:
                raise  # 남은 묶음도 막힌다. 읽은 것도 버려 since를 앞당기지 않는다(다음에 다시 읽음)
            except AdapterError as e:
                errors.append(e)
        if errors and not rows:
            raise errors[0]
        if errors:
            # 일부 글을 못 읽으면 그 글의 새 댓글을 다음 실행에서 놓칠 수 있다(since가 앞당겨짐)
            log.warning(
                "%s 댓글: 글 묶음 %d개를 읽지 못함: %s", self.platform, len(errors), errors[0]
            )
        out: list[Comment] = []
        for r in rows:
            if norm_handle(r["handle"]) == self.handle or not r.get("text"):
                continue  # 내 캡션·내 답글
            at = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
            if since is not None and at <= since:
                continue
            out.append(
                Comment(
                    platform="instagram",
                    external_id=f"{r['handle']}:{r['cid']}",
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
        author, cid = _split_comment_id(comment.external_id)
        args = {
            "parent": comment.post_external_id,
            "author": author,
            "cid": cid,
            "text": text,
            "probe": text.strip()[:20],
        }
        d = self._run(_REPLY, args) or {}
        if not d.get("ok"):
            log.warning("인스타그램: 답글은 보냈지만 확인하지 못함(%s)", comment.external_id)
            return ""  # 빈 값 = 확인 못 함. 엔진이 사람에게 넘긴다(C23)
        # 웹 화면에서 답글 ID를 안정적으로 얻을 수 없다. "화면에서 확인됨"을 뜻하는 표지만 돌려준다
        return f"seen:{comment.external_id}"

    def hide_comment(self, comment: Comment) -> None:
        self._need("hide_comment")  # 미지원(ADR 0018) → NotSupportedError

    def read_post_metrics(self, post_external_id: str) -> PostMetrics:
        self._need("post_metrics")
        d = self._run(_POST_METRICS, {"code": post_external_id}) or {}
        return PostMetrics(
            platform="instagram",
            post_external_id=post_external_id,
            captured_at=self._now(),
            views=d.get("views"),
            reach=d.get("reach"),
            likes=d.get("likes"),
            comments=d.get("comments"),
            saves=d.get("saves"),
            shares=d.get("shares"),
        )

    def read_account_metrics(self) -> AccountMetrics:
        self._need("account_metrics")
        d = self._run(_ACCOUNT_METRICS, {}) or {}
        return AccountMetrics(
            platform="instagram", captured_at=self._now(), followers=d.get("followers")
        )
