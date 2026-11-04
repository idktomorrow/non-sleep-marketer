"""C37: 플랫폼 오류 화면과 "조용한 빈 결과" 금지. 실제 Aside 스크립트를 node에서 가짜 탭(pg)·가짜 화면(document)에 대고
돌린다(가짜 Aside 실행기). 실계정·브라우저 없음. node가 없으면 건너뛴다.

시간은 가짜다: sleep이 가짜 시계를 앞으로 돌려 20초 기다림이 바로 끝난다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import pytest

from nsm.adapters.drivers.aside import AsideError, build_script, parse_output
from nsm.adapters.platforms import instagram as IG
from nsm.adapters.platforms import threads as TH
from nsm.adapters.platforms.aside_common import LoginRequiredError, PlatformError
from nsm.adapters.platforms.threads import ThreadsAdapter

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node 없음")

# 가짜 화면: scene = {"text": 화면 글, "buttons": [버튼 글], "btnBox": 버튼이 든 칸의 글, "counts": {선택자 조각: 개수}, "handle": 내 핸들}
_MOCK = r"""
const SCENE = __SCENE__;
let __now = 1_700_000_000_000;
let __tab = -1;  // 몇 번째로 연 탭인가(SCENE.seq: 탭마다 본문 컨테이너 수)
const containers = () => (SCENE.seq ? SCENE.seq[__tab] ?? 0 : (SCENE.counts.container || 0));
Date.now = () => __now;
globalThis.sleep = async (ms) => { __now += ms || 0; };
// 버튼의 부모 칸 글: 기본은 화면 글 전체(오류 안내 칸), SCENE.btnBox로 따로 줄 수 있다
const btn = (t) => ({ innerText: t, parentElement: { innerText: SCENE.btnBox ?? SCENE.text ?? '', parentElement: null }, getAttribute: () => null, querySelector: () => null, querySelectorAll: () => [] });
const meLink = { getAttribute: () => '/@' + SCENE.handle, querySelector: () => ({}) };
globalThis.document = {
  body: { get innerText() { return SCENE.text || ''; } },
  querySelectorAll(sel) {
    if (/role="button"|button/.test(sel)) return (SCENE.buttons || []).map(btn);
    if (sel.includes('a[href^="/@"]')) return SCENE.handle ? [meLink] : [];
    if (sel.includes('data-pressable-container')) return Array.from({ length: containers() }, () => btn(''));
    return [];
  },
  querySelector() { return null; },
};
function count(sel) {
  if (sel.includes('a[href^="/@"]')) return SCENE.handle ? 1 : 0;
  for (const [k, v] of Object.entries(SCENE.counts || {})) if (k !== 'container' && sel.includes(k)) return v;
  if (sel.includes('data-pressable-container')) return containers();
  return 0;
}
const fakePage = {
  url: () => SCENE.url || 'https://www.threads.com/@me/post/P1',
  evaluate: async (fn, arg) => fn(arg),
  locator: (sel) => ({ count: async () => count(sel), first() { return this; } }),
  goto: async () => {},
  screenshot: async () => null,
};
globalThis.openTab = async () => { __tab += 1; return fakePage; };
globalThis.closeTab = async () => {};
globalThis.fs = { writeFile: async () => {} };
"""


class NodeRepl:
    """가짜 Aside 실행기: 스크립트를 가짜 화면과 함께 node로 돌리고, 진짜 실행기처럼 결과 줄을 읽는다."""

    def __init__(self, scene: dict[str, Any]) -> None:
        self.scene = scene

    def available(self) -> bool:
        return True

    def run(self, body: str, args=None, files=None):
        src = _MOCK.replace("__SCENE__", json.dumps(self.scene)) + build_script(
            body, {**(args or {}), "__files": []}
        )
        proc = subprocess.run(  # noqa: S603  고정된 node + 임시 스크립트(표준 입력)
            [NODE, "--input-type=module", "-"],
            input=src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        res = parse_output(proc.stdout, proc.stderr, proc.returncode)
        if not res.get("ok"):
            raise AsideError(
                res.get("error", ""), code=res.get("code") or "script", evidence=res.get("evidence")
            )
        return res.get("data")


ERROR_SCREEN = "스레드\n문제가 발생했습니다. 나중에 다시 시도해보세요.\n다시 시도"


def _threads(clock, scene) -> ThreadsAdapter:
    return ThreadsAdapter("@me", clock, repl=NodeRepl({"handle": "me", **scene}))


def test_error_screen_is_transient_page_error(clock) -> None:  # (a)
    a = _threads(
        clock, {"text": ERROR_SCREEN, "buttons": ["다시 시도"], "counts": {"container": 0}}
    )
    with pytest.raises(PlatformError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.kind == "transient" and "플랫폼이 오류 화면을 띄움" in str(e.value)


def test_error_words_in_a_comment_without_retry_button_are_not_an_error_screen(clock) -> None:
    """댓글에 '문제가 발생했습니다'가 있어도 '다시 시도' 버튼은 없다: 오류 화면이 아니다."""
    a = _threads(
        clock,
        {
            "text": "댓글: 앱에서 문제가 발생했습니다 ㅠ",
            "buttons": ["답글"],
            "counts": {"container": 1},
        },
    )
    assert a.fetch_new_comments(["P1"], None) == []


def test_body_never_loads_is_an_error_not_empty_list(clock) -> None:  # (b)
    a = _threads(clock, {"text": "스레드\n홈\n검색", "counts": {"container": 0}})
    with pytest.raises(PlatformError) as e:
        a.fetch_new_comments(["P1", "P2"], None)
    assert e.value.kind == "transient" and "게시물 본문이 20초 안에 열리지 않음" in str(e.value)


def test_body_loaded_with_no_comments_is_an_empty_list(clock) -> None:  # (c)
    a = _threads(
        clock, {"text": "스레드\n내 글 본문", "counts": {"container": 1}}
    )  # 본문 글 하나뿐
    assert a.fetch_new_comments(["P1"], None) == []


def test_error_screen_while_waiting_for_login_is_not_a_logout(clock) -> None:
    """로그인 확인을 기다리는 사이 오류 화면이 뜨면 로그인 문제가 아니다(E5 빈 화면 판정보다 먼저)."""
    a = ThreadsAdapter(
        "@me",
        clock,
        repl=NodeRepl({"handle": "", "text": ERROR_SCREEN, "buttons": ["다시 시도"], "counts": {}}),
    )
    with pytest.raises(PlatformError) as e:
        a.fetch_new_comments(["P1"], None)
    assert not isinstance(e.value, LoginRequiredError) and e.value.kind == "transient"


def test_login_form_is_still_a_login_problem(clock) -> None:
    """로그인 폼에는 오류 안내문이 없다: 그대로 로그인 필요."""
    a = ThreadsAdapter(
        "@me",
        clock,
        repl=NodeRepl(
            {
                "handle": "",
                "text": "Instagram으로 계속\n로그인",
                "buttons": ["로그인"],
                "counts": {},
            }
        ),
    )
    with pytest.raises(LoginRequiredError):
        a.fetch_new_comments(["P1"], None)


def test_every_script_parses() -> None:
    """고친 스크립트가 모두 node에서 읽힌다(문법 오류는 실계정에서야 드러난다)."""
    bodies = [
        v
        for m in (IG, TH)
        for k, v in vars(m).items()
        if k.startswith("_") and isinstance(v, str) and "nsmOpen(" in v
    ]
    assert len(bodies) >= 10
    for b in bodies:
        r = subprocess.run(  # noqa: S603
            [NODE, "--check", "--input-type=module", "-"],
            input=build_script(b, {}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert r.returncode == 0, r.stderr[:300]


def test_retry_button_elsewhere_does_not_make_an_error_screen(clock) -> None:
    """리뷰: 안내문과 '다시 시도' 버튼이 다른 칸에 있으면(본문은 정상) 오류 화면이 아니다."""
    a = _threads(
        clock,
        {
            "text": "내 글 본문\n댓글: 문제가 발생했습니다 ㅠ\n다시 시도",
            "buttons": ["다시 시도"],
            "btnBox": "다시 시도",
            "counts": {"container": 1},
        },
    )
    assert a.fetch_new_comments(["P1"], None) == []


def test_one_post_that_never_loads_is_skipped_others_are_read(clock) -> None:
    """리뷰: 지운 글처럼 하나만 안 열리면 그 글만 건너뛴다(조회 전체를 막지 않는다). 모두 안 열리면 오류."""
    scene = {"handle": "me", "text": "스레드", "counts": {"container": 0}}
    a = ThreadsAdapter("@me", clock, repl=NodeRepl(scene))
    with pytest.raises(PlatformError):
        a.fetch_new_comments(["P1", "P2"], None)  # 둘 다 안 열림
    # 첫 글은 본문이 뜨고(댓글 0개) 둘째 글만 안 뜨는 화면
    seq = NodeRepl({**scene, "seq": [1, 0]})
    b = ThreadsAdapter("@me", clock, repl=seq)
    assert b.fetch_new_comments(["P1", "P2"], None) == []
