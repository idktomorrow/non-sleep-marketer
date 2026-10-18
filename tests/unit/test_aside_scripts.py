"""Aside 고정 스크립트(JS 템플릿)의 구조 검사 (배치 C21).

브라우저 없이 돌릴 수 있는 범위: 안내 대화상자 닫기가 들어 있고, 업로드 흐름이 그것을 부르는지.
실제 화면 확인은 운영 계정에 올리는 계약 테스트(-m contract)와 수동 진단으로 한다.
"""

from __future__ import annotations

from nsm.adapters.platforms import aside_common, instagram, threads


def test_common_helpers_define_prompt_dismissal() -> None:
    js = aside_common.JS_ASSERT_HANDLE
    assert "async function nsmDismissPrompts(pg)" in js
    assert "'나중에 하기'" in js and "'Not Now'" in js
    # 알림 안내 대화상자만 닫는다(업로드 창의 "취소" 같은 버튼은 건드리지 않음)
    assert "/알림|Notification/i.test(heading)" in js


def test_mark_icon_searches_every_matching_scope() -> None:
    js = aside_common.JS_ASSERT_HANDLE
    body = js.split("async function nsmMarkIcon")[1].split("async function")[0]
    assert "document.querySelectorAll(scopeSel)" in body
    assert "document.querySelector(scopeSel)" not in body


def test_instagram_publish_dismisses_prompt_before_crop() -> None:
    js = instagram._PUBLISH
    before_crop, after = js.split("'자르기 선택'")
    assert "await nsmDismissPrompts(pg)" in before_crop
    # 자르기 아이콘을 기다리는 동안에도 닫는다(안내 창이 늦게 떠도 됨)
    assert (
        "await nsmDismissPrompts(pg); return nsmMarkIcon(pg, '[role=\"dialog\"]', '자르기 선택'"
        in js
    )
    # 다음 단계로 넘어갈 때도 닫는다
    assert (
        "await nsmDismissPrompts(pg);"
        in js.split("async function nextStep")[1].split("return false;")[0]
    )


def test_scripts_still_assert_handle_first() -> None:
    for js in (instagram._PUBLISH, threads.__dict__.get("_PUBLISH", "")):
        if js:
            assert js.index("nsmAssertHandle") < js.index("nsmFiles")
