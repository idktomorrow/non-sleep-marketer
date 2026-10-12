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

BASE = "https://www.threads.com"
COMMENT_BATCH = 4  # repl 한 번에 읽을 글 수(글당 약 10초)
IDEMPOTENCY_WINDOW = timedelta(hours=6)  # 재시도 간격 합(15분·1시간·4시간)보다 넉넉히

JS_HANDLE = (
    JS_ASSERT_HANDLE
    + r"""
const MAIN = '[aria-label="칼럼 본문"]';  // 가운데 칼럼(왼쪽 메뉴의 같은 이름 아이콘과 구분)
async function readHandle(pg) {
  await nsmUntil(async () => (await pg.locator('a[href^="/@"]').count()) > 0, 20000);
  return await pg.evaluate(() => {
    for (const a of document.querySelectorAll('a[href^="/@"]')) {
      const lab = a.querySelector('svg[aria-label="프로필"], svg[aria-label="Profile"]');
      if (lab) return a.getAttribute('href').slice(2).split('/')[0];
    }
    return '';
  });
}
"""
)

_HEALTH = (
    JS_HANDLE
    + r"""
const pg = await nsmOpen('https://www.threads.com/');
const me = ((await readHandle(pg)) || '').toLowerCase();
return { me };
"""
)

_FIND_MINE = r"""
// 프로필 최신 글 중 고유 문구(probe)를 가진 내 글의 코드
async function findMine(pg) {
  await pg.goto('https://www.threads.com/@' + ARGS.handle);
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

_PUBLISH = (
    JS_HANDLE
    + _FIND_MINE
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
  if (!ok) throw new NsmError('script', '이미지 미리보기가 나타나지 않음');
}
if (!(await nsmClickText(dlg, '게시'))) throw new NsmError('script', '게시 버튼 없음');
// 게시 버튼을 누른 뒤에는 오류로 돌리지 않는다: 오류 → 재시도 → 중복 게시가 될 수 있다.
const closed = await nsmUntil(async () => (await pg.locator('[role="dialog"] [contenteditable="true"]').count()) === 0, 90000);
if (!closed) return { code: '', posted: 'unknown' };
const found = ARGS.probe ? await nsmUntil(() => findMine(pg), 45000, 3000) : '';
return { code: found || '', posted: true };
"""
)

_DELETE = (
    JS_HANDLE
    + r"""
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + ARGS.code);
await nsmAssertHandle(pg, readHandle);
// 답글이면 위에 원글 맥락이 함께 보인다. 메뉴를 차례로 열어 "삭제"가 있는 것(내 글)을 고른다.
await nsmUntil(() => nsmMarkIcon(pg, MAIN, '더 보기', 0), 30000);
let done = false;
for (let i = 0; i < 4 && !done; i++) {
  const more = await nsmMarkIcon(pg, MAIN, '더 보기', i);
  if (!more) break;
  await more.click();
  await sleep(800);
  done = await nsmClickText(pg, '삭제');
  if (!done) { await pg.keyboard.press('Escape'); await sleep(400); }
}
if (!done) throw new NsmError('script', '삭제 메뉴 없음');
await sleep(800);
const dlg = pg.locator('[role="dialog"]');
if (!(await nsmClickText(dlg, '삭제'))) throw new NsmError('script', '삭제 확인 버튼 없음');
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
await nsmUntil(async () => (await pg.locator('svg title').count()) > 0 && await nsmMarkIcon(pg, MAIN, '좋아요', 0), 30000);
const counts = {};
// 글 페이지에서 처음 나오는 동작 버튼들이 본문 글의 것이다(답글들은 그 아래)
for (const lab of ['좋아요', '답글', '리포스트', '공유하기']) {
  const b = await nsmMarkIcon(pg, MAIN, lab, 0);
  counts[lab] = b ? ((await b.innerText()) || '').trim() : null;
}
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
const t = await nsmUntil(async () => {
  const s = await pg.evaluate(() => Array.from(document.querySelectorAll('[role="button"], a')).map(b => (b.innerText || '').trim()).find(x => /^팔로워/.test(x)) || '');
  return s;
}, 20000);
return { followers: t ? nsmNum(t.replace('팔로워', '')) : null };
"""
)


# 글 페이지의 답글 목록: 첫 컨테이너는 본문 글, 그다음부터 답글(각자 /post/<code>를 가진 글)
_COMMENTS = (
    JS_HANDLE
    + r"""
const out = [];
for (const code of ARGS.codes) {
  const pg = await nsmOpen('https://www.threads.com/@' + ARGS.handle + '/post/' + code);
  if (code === ARGS.codes[0]) await nsmAssertHandle(pg, readHandle);
  await nsmUntil(async () => (await pg.locator(MAIN + ' [data-pressable-container="true"]').count()) > 0, 20000);
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
  await closeTab(pg); __opened.splice(__opened.indexOf(pg), 1);
}
return out;
"""
)

_REPLY = (
    JS_HANDLE
    + r"""
// 댓글 자체 페이지: 위에 내 글(답글 아이콘 0번), 그 아래 댓글(1번). 1번을 누르면 "답글" 창이 열린다.
const pg = await nsmOpen('https://www.threads.com/@' + ARGS.author + '/post/' + ARGS.code);
await nsmAssertHandle(pg, readHandle);
const icon = await nsmUntil(() => nsmMarkIcon(pg, MAIN, '답글', 1), 30000);
if (!icon) throw new NsmError('script', '댓글의 답글 버튼 없음');
await icon.click();
await nsmWait(pg.locator('[role="dialog"] [contenteditable="true"]'));
// 창에 인용된 원글이 그 댓글(작성자)인지 확인하고 쓴다
if ((await pg.locator('[role="dialog"] a[href="/@' + ARGS.author + '"]').count()) === 0) throw new NsmError('script', '답글 창의 대상이 다름');
const dlg = pg.locator('[role="dialog"]');
const box = await nsmWait(dlg.locator('[contenteditable="true"]'));
await box.click();
await pg.keyboard.insertText(ARGS.text);
if (!(await nsmClickText(dlg, '게시'))) throw new NsmError('script', '답글 게시 버튼 없음');
// 여기부터는 답글이 나갔을 수 있으니 오류로 돌리지 않는다(사람이 다시 답하면 중복)
const closed = await nsmUntil(async () => (await pg.locator('[role="dialog"] [contenteditable="true"]').count()) === 0, 60000);
if (!closed) return { code: '' };
const mine = await nsmUntil(async () => {
  await pg.goto('https://www.threads.com/@' + ARGS.author + '/post/' + ARGS.code);
  await sleep(2500);
  return await pg.evaluate((args) => {
    for (const c of document.querySelectorAll('[data-pressable-container="true"]')) {
      const h = Array.from(c.querySelectorAll('a[href^="/@' + args.handle + '/post/"]')).map(a => a.getAttribute('href'))[0];
      if (h && (c.innerText || '').includes(args.probe)) return h.split('/post/')[1].split(/[/?]/)[0];
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
await nsmUntil(async () => (await pg.locator(MAIN + ' a[href$="/post/' + ARGS.code + '"]').count()) > 0, 30000);
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
if (!ok) throw new NsmError('script', '답글 메뉴(더 보기) 없음');
await pg.locator('[data-nsm-mark="' + key + '"]').click();
await sleep(800);
if (!(await nsmClickText(pg, '모두에게 숨기기'))) throw new NsmError('script', '"모두에게 숨기기" 메뉴 없음');
await sleep(1200);
const dlg = pg.locator('[role="dialog"]');
if ((await dlg.count()) > 0) {
  if (!(await nsmClickText(dlg, '숨기기'))) throw new NsmError('script', '숨기기 확인 버튼 없음');
  await sleep(1200);
}
// 숨긴 답글은 목록에서 빠진다. 그대로 보이면 숨기지 못한 것이다.
await pg.reload(); await sleep(2500);
if ((await pg.locator('a[href$="/post/' + ARGS.code + '"]').count()) > 0) throw new NsmError('script', '숨긴 뒤에도 댓글이 보임');
return { hidden: true };
"""
)


def _split_comment_id(external_id: str) -> tuple[str, str]:
    """댓글 ID는 "작성자:코드". 답글도 작성자 주소 아래의 글이라 둘 다 있어야 연다.

    "/"는 쓰지 않는다: 대시보드가 "플랫폼:ID"를 URL 경로에 넣는다(/comments/{key}/reply).
    """
    author, _, code = external_id.partition(":")
    if not author or not code:
        raise AdapterError(f"스레드 댓글 ID 형식이 아님: {external_id}")
    return author, code


def _probe(text: str) -> str:
    """프로필에서 이 글을 알아볼 고유 문구: 표기 줄·해시태그 줄을 뺀 본문 앞 40자.

    너무 짧으면(8자 미만) 빈 문자열: 다른 글과 헷갈릴 수 있어 멱등 확인을 하지 않는다.
    """
    lines = [ln.strip() for ln in text.splitlines()[1:]]
    body = " ".join(ln for ln in lines if ln and not ln.startswith("#"))
    return body[:40] if len(body) >= 8 else ""


class ThreadsAdapter(AsideAdapterBase):
    platform = "threads"

    def health(self) -> HealthStatus:
        me = (self.repl.run(_HEALTH, {}) or {}).get("me", "")
        if not me:
            return HealthStatus(logged_in=False, detail="스레드 로그인 필요(Aside 브라우저)")
        if self.handle and me != self.handle:
            return HealthStatus(logged_in=False, detail=f"로그인 계정 @{me} ≠ 설정 @{self.handle}")
        return HealthStatus(logged_in=True, detail=f"@{me}")

    def publish(self, post: RenderedPost) -> PublishResult:
        self._need("publish")
        images = check_upload_paths(post.images, self.media_dir)
        since = (self._now() - IDEMPOTENCY_WINDOW).isoformat()
        args = {"text": post.text, "probe": _probe(post.text), "since": since}
        data = self._run(_PUBLISH, args, images) or {}
        code = data.get("code", "")
        if data.get("existing"):
            log.warning("스레드: 지난 시도에서 이미 올라간 글을 찾아 다시 올리지 않음(%s)", code)
        if not code:
            # 올라갔지만 주소를 못 찾음: 다시 올리면 중복이므로 성공으로 기록하고 프로필 주소를 남긴다.
            # external_id가 비면 댓글·지표 수집에서 빠진다.
            log.warning("스레드: 게시는 됐지만 글 주소를 찾지 못함(%s)", post.content_id)
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
        ids = list(post_external_ids)
        # 한 번의 repl(120초)에 글 몇 개만: 글이 쌓여도 제한에 걸려 통째로 실패하지 않게
        for i in range(0, len(ids), COMMENT_BATCH):
            try:
                rows += self._run(_COMMENTS, {"codes": ids[i : i + COMMENT_BATCH]}) or []
            except AdapterError as e:
                errors.append(e)
        if errors and not rows:
            raise errors[0]
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
                "probe": text.strip()[:20],
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
