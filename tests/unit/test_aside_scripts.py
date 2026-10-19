"""Aside 고정 스크립트(JS 템플릿)의 구조 검사 (배치 C21).

브라우저 없이 돌릴 수 있는 범위: 안내 대화상자 닫기가 들어 있고, 업로드 흐름이 그것을 올바른 순서로 부르는지.
실제 화면 확인은 운영 계정에 올리는 계약 테스트(-m contract)와 수동 진단으로 한다.
"""

from __future__ import annotations

import re

from nsm.adapters.platforms import aside_common, instagram, threads


def _between(js: str, start: str, end: str) -> str:
    i = js.index(start)
    return js[i : js.index(end, i)]


def test_common_helpers_define_prompt_dismissal() -> None:
    js = aside_common.JS_ASSERT_HANDLE
    body = _between(js, "async function nsmDismissPrompts", "async function nsmUntil")
    assert "나중에 하기" in js and "Not Now" in js
    # 알림 안내 대화상자만 닫는다(업로드 창의 "취소" 같은 버튼은 건드리지 않음). 제목 요소가 없어도 첫 줄로 본다
    assert re.search(r"/알림\|Notification[^/]*/i\.test\(heading\)", body)
    assert '[role="heading"]' in body and "split(" in body


def test_mark_icon_searches_every_matching_scope_once() -> None:
    body = _between(
        aside_common.JS_ASSERT_HANDLE, "async function nsmMarkIcon", "nsmDismissPrompts"
    )
    assert "document.querySelectorAll(scopeSel)" in body
    assert "document.querySelector(scopeSel)" not in body
    assert (
        "new Set(" in body
    )  # 겹치는 scope에서 같은 아이콘이 두 번 세어지면 nth가 밀린다(스레드 답글 대상)


def test_instagram_publish_order_and_dismissal() -> None:
    js = instagram._PUBLISH
    # 계정 확인 → 멱등 확인(이미 올라갔나) → 파일 받기 → 안내 창 닫기 → 파일 넣기 → 자르기
    order = [
        "await nsmAssertHandle(pg, readHandle)",
        "findMine(pg, 2)",
        "await nsmFiles()",
        "await nsmDismissPrompts(pg)",
        "setInputFiles(files)",
        "'자르기 선택'",
    ]
    # 앞 단계 뒤에서부터 찾는다(nsmDismissPrompts는 도우미 nextStep 안에도 먼저 나오므로)
    pos = -1
    for s in order:
        pos = js.index(s, pos + 1)
    # 자르기 아이콘을 기다리는 동안에도 닫는다(안내 창이 늦게 떠도 됨)
    crop_wait = _between(js, "const crop = await nsmUntil(", "30000)")
    assert "nsmDismissPrompts(pg)" in crop_wait and "'자르기 선택'" in crop_wait
    # 다음 단계로 넘어갈 때도 닫는다
    next_step = _between(js, "async function nextStep", "return false;")
    assert "nsmDismissPrompts(pg)" in next_step
    assert next_step.index("nsmDismissPrompts(pg)") < next_step.index(
        "clickDialogButton(pg, '다음')"
    )


def test_instagram_reply_does_not_click_the_box_after_reply_button() -> None:
    """C22: "답글 달기" 뒤에 입력창을 마우스로 클릭하면 답글 대상이 풀려 새 댓글이 된다(2026-10-19 실험)."""
    js = instagram._REPLY
    seg = _between(js, "'답글 달기'", "insertText(ARGS.text)")
    assert ".click();" in seg.split("nsmWait(")[0]  # 답글 달기 버튼은 누른다
    after_box = seg.split("nsmWait(")[1]
    assert "box.click()" not in after_box and "press('End')" not in after_box
    assert (
        "startsWith('@' + ARGS.author)" in after_box
    )  # 채워진 "@작성자"로 답글 상태를 확인한 뒤 쓴다
    assert "e.focus()" in after_box


def test_threads_publish_still_asserts_handle_before_upload() -> None:
    js = threads._PUBLISH
    assert js.index("await nsmAssertHandle(pg, readHandle)") < js.index("await nsmFiles()")
