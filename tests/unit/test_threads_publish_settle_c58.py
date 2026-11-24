"""C58: 스레드 게시 뒤 게시 탭을 떠나지 않고, 확인은 따로 연 탭에서(ADR 0078, incidents/0017).

스크립트 구조는 문자열로, 도우미(nsmClickText·nsmEvidence)의 동작은 node 가짜 실행기로 본다.
실계정·브라우저 없음. node가 없으면 node 테스트만 건너뛴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nsm.adapters.drivers import aside as drv
from nsm.adapters.drivers.aside import build_script
from nsm.adapters.platforms import aside_common, instagram, threads

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node 없음")


def _after(js: str, marker: str) -> str:
    return js[js.index(marker) :]


def test_publish_waits_on_post_tab_then_finds_on_check_tab() -> None:
    js = threads._PUBLISH
    order = [
        "const closed = await nsmUntil(",
        "if (!closed) return",
        "await sleep(5000)",
        "POSTING.test(t)",
        "chk = await nsmOpen(",
        "findMine(chk)",
        "const wait = Math.min(ARGS.find_ms, nsmLeft() - 25000)",
        "await nsmUntil(findOnCheckTab, wait, 3000)",
    ]
    pos = -1
    for s in order:
        pos = js.index(s, pos + 1)
    after_click = _after(js, "nsmClickText(dlg, '게시')")
    # 게시 탭(pg)은 게시 뒤 다른 주소로 옮기지 않는다: findMine(pg)도, pg.goto도 없다
    assert "findMine(pg)" not in after_click and "pg.goto" not in after_click
    # findMine 자체의 goto는 넘겨받은 탭에서만
    assert "await pg.goto(" in threads._FIND_MINE  # 함수 인자 이름
    # 게시 전 멱등 확인은 그대로 게시 탭에서
    assert js.index("const before = ARGS.probe ? await findMine(pg)") < js.index("nsmFiles()")
    # 증거는 게시 탭에서 찍는다
    assert "nsmEvidence({ page: pg })" in after_click
    # 옛 예산(17초 여유)은 남지 않는다
    assert "nsmLeft() - 17000" not in js


def test_publish_status_phrases_are_optional() -> None:
    """'게시 중'이 보이면 기다리고 '게시됨'이면 바로 넘어간다. 아무 문구가 없어도 실패가 아니다(최대 15초)."""
    js = threads._PUBLISH
    assert "/게시 중|Posting/" in js and "/게시됨|게시되었|Posted/" in js
    seg = js[js.index("await sleep(5000)") : js.index("let chk = null")]
    assert "}, 15000, 1000)" in seg
    assert "throw" not in seg  # 문구 대기는 실패로 만들지 않는다


def test_publish_click_waits_for_enabled_button_after_preview() -> None:
    js = threads._PUBLISH
    seg = js[js.index("setInputFiles(files)") : js.index("const closed")]
    assert seg.index('img[src^="blob:"], video') < seg.index("await sleep(1500)")
    assert seg.index("await sleep(1500)") < seg.index("nsmClickText(dlg, '게시')")
    assert "await nsmUntil(() => nsmClickText(dlg, '게시'), 15000, 500)" in seg


def test_replies_retry_the_post_button() -> None:
    assert "nsmUntil(() => nsmClickText(dlg, '게시'), 5000, 300)" in threads._REPLY
    assert "nsmUntil(() => nsmClickText(pg.locator('main'), '게시'), 5000, 300)" in instagram._REPLY


def test_click_text_skips_disabled_in_source() -> None:
    js = aside_common.JS_ASSERT_HANDLE
    body = js[js.index("async function nsmClickText") : js.index("async function nsmMarkIcon")]
    assert "aria-disabled" in body and "'true'" in body and "hasAttribute('disabled')" in body


# ---- node 가짜 실행기 ----

_HARNESS = r"""
let __now = 1_700_000_000_000;
Date.now = () => __now;
globalThis.sleep = async (ms) => { __now += ms || 0; };
const clicks = [];
// 버튼: {t: 글, aria: aria-disabled 값, dis: disabled 속성}. 3번째 확인부터 '게시'가 활성이 된다(enableAt)
let polls = 0;
function scopeOf(buttons, enableAt) {
  return {
    locator() {
      polls += 1;
      const list = buttons.map((b) => ({ ...b, aria: b.lateEnable && polls >= enableAt ? null : b.aria }));
      return {
        count: async () => list.length,
        nth: (i) => ({
          innerText: async () => list[i].t,
          evaluate: async (fn) => fn({
            getAttribute: (n) => (n === 'aria-disabled' ? (list[i].aria ?? null) : null),
            hasAttribute: (n) => n === 'disabled' && !!list[i].dis,
          }),
          click: async () => { clicks.push(i); },
        }),
      };
    },
  };
}
"""


def _run_node(tmp_path: Path, body: str) -> dict:
    js = (
        _HARNESS  # 가짜 시계를 먼저 세우고 그 시계로 마감을 정한다
        + "const __deadline = Date.now() + 105000;\n"
        + "function nsmLeft() { return Math.max(0, __deadline - Date.now()); }\n"
        + _helper("nsmClickText")
        + _helper("nsmUntil")
        + "\n(async () => {\n"
        + body
        + "\n})().then((r) => console.log(JSON.stringify(r)));\n"
    )
    f = tmp_path / "t.mjs"
    f.write_text(js, encoding="utf-8")
    out = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _helper(name: str) -> str:
    js = aside_common.JS_ASSERT_HANDLE
    i = js.index(f"async function {name}(")
    j = js.find("\nasync function ", i + 1)
    k = js.find("\n// ", i + 1)
    ends = [x for x in (j, k) if x != -1]
    return js[i : min(ends) if ends else len(js)] + "\n"


@needs_node
def test_click_text_skips_disabled_buttons_in_node(tmp_path) -> None:
    r = _run_node(
        tmp_path,
        """
const s = scopeOf([{ t: '게시', aria: 'true' }, { t: '게시', dis: true }, { t: '취소' }], 0);
const first = await nsmClickText(s, '게시');
const s2 = scopeOf([{ t: '게시', aria: 'true' }, { t: '게시' }], 0);
const second = await nsmClickText(s2, '게시');
return { first, second, clicks };
""",
    )
    assert r == {"first": False, "second": True, "clicks": [1]}


@needs_node
def test_until_click_waits_until_button_becomes_enabled(tmp_path) -> None:
    r = _run_node(
        tmp_path,
        """
const s = scopeOf([{ t: '게시', aria: 'true', lateEnable: true }], 3);
const ok = await nsmUntil(() => nsmClickText(s, '게시'), 15000, 500);
return { ok, clicks, polls };
""",
    )
    assert r["ok"] is True and r["clicks"] == [0] and r["polls"] == 3


@needs_node
def test_evidence_shoots_the_given_tab_not_the_last_opened(tmp_path) -> None:
    prelude = drv._PRELUDE.replace("__PAYLOAD__", json.dumps("e30="))  # base64("{}")
    body = """
const shot = [];
const mk = (name) => ({ url: () => name, evaluate: async () => ({ dialogs: 0, labels: [], text: name }),
  screenshot: async (o) => { shot.push([name, o.timeout]); return Buffer.from('x'); } });
globalThis.openTab = async (u) => mk(u);
const post = await nsmOpen('post');
const check = await nsmOpen('check');
const a = await nsmEvidence({ page: post });
const b = await nsmEvidence();
console.log(JSON.stringify({ a: a.url, b: b.url, shot }));
"""
    f = tmp_path / "e.mjs"
    f.write_text(prelude + body, encoding="utf-8")
    out = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["a"] == "post" and r["b"] == "check"
    assert r["shot"][0] == ["post", 5000]  # 시간이 넉넉하면 화면 예산 5초를 온전히 받는다


@needs_node
@pytest.mark.parametrize(
    "body",
    [threads._PUBLISH, threads._REPLY, instagram._REPLY],
    ids=["threads_publish", "threads_reply", "instagram_reply"],
)
def test_changed_scripts_parse(tmp_path, body) -> None:
    f = tmp_path / "s.mjs"
    f.write_text(build_script(body, {}), encoding="utf-8")
    out = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
