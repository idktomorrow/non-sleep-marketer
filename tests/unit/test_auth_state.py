"""E5: 로그인 안 됨의 세부 상태 분류(ADR 0060). 고정 입력은 실제 증거 네 건(tests/fixtures/auth_states).

실계정 호출 없음: 가짜 repl이 스크립트 결과를 돌려준다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.platforms.aside_common import LoginRequiredError, PlatformError
from nsm.adapters.platforms.auth_state import AUTH_STATES, _rules, classify, is_blank
from nsm.adapters.platforms.instagram import _HEALTH as IG_HEALTH
from nsm.adapters.platforms.instagram import JS_IG, InstagramAdapter
from nsm.adapters.platforms.threads import _HEALTH as TH_HEALTH
from nsm.adapters.platforms.threads import JS_HANDLE, ThreadsAdapter
from nsm.domain.models import HealthStatus

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "auth_states"


def _ev(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text("utf-8"))


class FakeRepl:
    def __init__(self, data=None, error: AsideError | None = None) -> None:
        self.data, self.error = data, error
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        self.calls.append(body)
        if self.error:
            raise self.error
        return self.data


# ------------------------------------------------------------------ 분류: 실제 증거 네 건


@pytest.mark.parametrize(
    ("name", "state", "blank"),
    [
        (
            "instagram-20261029-085642",
            "challenge",
            False,
        ),  # 사람임을 확인하세요(주소는 /accounts/suspended/)
        ("instagram-20261030-032618", "verify", False),  # 휴대폰 번호를 입력하세요 / 코드 보내기
        ("instagram-20261101-032543", "verify", False),
        ("threads-20261102-235339", "unknown", True),  # 정상 글 주소인데 글·라벨이 빈 하얀 화면
    ],
)
def test_real_evidence(name: str, state: str, blank: bool) -> None:
    ev = _ev(name)
    assert classify(ev) == state
    assert is_blank(ev) is blank


def test_suspended_url_alone_is_not_restricted() -> None:
    """인스타그램은 본인 확인·문자 인증도 /accounts/suspended/로 띄운다: 주소만으론 restricted가 아니다."""
    url = "https://www.instagram.com/accounts/suspended/?next=%2F"
    assert classify({"url": url, "text": "계속", "labels": ["Menu"]}) == "unknown"
    assert (
        classify({"url": url, "text": "계정이 일시적으로 정지되었습니다", "labels": []})
        == "restricted"
    )
    assert classify({"url": url, "text": "We suspended your account", "labels": []}) == "restricted"


@pytest.mark.parametrize(
    ("url", "text", "state"),
    [
        (
            "https://www.instagram.com/accounts/login/",
            "전화번호, 사용자 이름 또는 이메일\n비밀번호\n로그인",
            "logged_out",
        ),
        (
            "https://www.instagram.com/",
            "Instagram\n로그인\n가입하기",
            "logged_out",
        ),  # 홈에 로그인 폼
        ("https://www.threads.com/login", "Instagram으로 계속", "logged_out"),
        ("https://www.instagram.com/challenge/abc/", "", "challenge"),  # 문구가 없으면 주소로
        ("https://www.instagram.com/accounts/login/two_factor?next=%2F", "", "two_factor"),
        ("https://www.instagram.com/", "2단계 인증\n인증 앱의 코드를 입력하세요", "two_factor"),
        (
            "https://www.instagram.com/",
            "이메일 주소를 확인하세요. 인증 코드를 보냈습니다",
            "verify",
        ),
        ("https://www.instagram.com/", "Help us confirm you're human", "challenge"),
        ("https://www.instagram.com/", "무언가 이상해 보입니다", "unknown"),
    ],
)
def test_url_and_text_combinations(url: str, text: str, state: str) -> None:
    assert classify({"url": url, "text": text, "labels": []}) == state


def test_text_wins_over_url() -> None:
    """화면 문구를 주소보다 먼저: 로그인 주소 위의 본인 확인 창은 challenge."""
    assert (
        classify(
            {"url": "https://www.instagram.com/accounts/login/", "text": "사람임을 확인하세요"}
        )
        == "challenge"
    )


def test_blank_needs_all_three_conditions() -> None:
    post = "https://www.threads.com/@me/post/X"
    assert is_blank({"url": post, "text": "", "labels": []})
    assert not is_blank({"url": post, "text": "", "labels": ["로그인"]})  # 라벨이 있으면 아님
    assert not is_blank({"url": post, "text": "로그인", "labels": []})  # 로그인 폼은 글이 있다
    assert not is_blank(
        {"url": "https://www.instagram.com/accounts/login/", "text": "", "labels": []}
    )
    assert not is_blank({"url": "", "text": "", "labels": []})  # 주소도 모르면 판단하지 않음
    assert not is_blank(None)


def test_patterns_live_in_one_data_file() -> None:
    r = _rules()
    states = {s for s, _ in r["text"]} | {s for s, _ in r["url"]}
    assert states <= set(AUTH_STATES)


# ------------------------------------------------------------------ 어댑터: health


def test_health_keeps_state_and_saves_evidence_without_exception(clock, _isolated_evidence) -> None:
    a = InstagramAdapter(
        "@nonsleepmarketer",
        clock,
        repl=FakeRepl({"me": "", "page": _ev("instagram-20261029-085642")}),
    )
    h = a.health()
    assert h == HealthStatus(logged_in=False, detail=h.detail) and not h.logged_in
    assert a.auth_state == "challenge"
    saved = list(_isolated_evidence.iterdir())
    assert len(saved) == 1 and f"(증거: {saved[0].name})" in h.detail  # 예외가 없어도 증거
    info = json.loads((saved[0] / "dialogs.json").read_text("utf-8"))
    assert info["kind"] == "auth" and "사람임을 확인" in info["text"]
    a.health()  # 한 시간 안: 다시 남기지 않는다(E1 쿨다운 그대로)
    assert len(list(_isolated_evidence.iterdir())) == 1
    a.repl.data = {"me": "nonsleepmarketer"}
    assert a.health().logged_in and a.auth_state is None


def test_plain_logout_saves_no_evidence(clock, _isolated_evidence) -> None:
    page = {"url": "https://www.threads.com/login", "text": "Instagram으로 계속", "labels": []}
    a = ThreadsAdapter("@nonsleepmarketer", clock, repl=FakeRepl({"me": "", "page": page}))
    assert not a.health().logged_in and a.auth_state == "logged_out"
    assert not _isolated_evidence.exists() or not list(_isolated_evidence.iterdir())


def test_health_blank_page_is_transient(clock) -> None:
    a = ThreadsAdapter(
        "@nonsleepmarketer",
        clock,
        repl=FakeRepl({"me": "", "page": _ev("threads-20261102-235339")}),
    )
    with pytest.raises(PlatformError) as e:
        a.health()
    assert e.value.kind == "transient" and not isinstance(e.value, LoginRequiredError)


def test_health_without_page_keeps_old_behaviour(clock) -> None:
    a = ThreadsAdapter("@nonsleepmarketer", clock, repl=FakeRepl({"me": ""}))
    assert not a.health().logged_in and a.auth_state is None


def test_health_scripts_only_open_home_and_never_click() -> None:
    """본인 확인·제한 상태에서 계정을 더 건드리지 않게: 홈 하나만 열고 로그인 페이지를 열거나 누르지 않는다."""
    for body, prefix in ((IG_HEALTH, JS_IG), (TH_HEALTH, JS_HANDLE)):
        own = body[len(prefix) :]
        assert own.count("nsmOpen(") == 1 and "goto(" not in own and ".click(" not in own
        assert "login" not in own.lower() and "nsmEvidence({ noShot: !ARGS.shot })" in own


# ------------------------------------------------------------------ 어댑터: 댓글 확인·게시 전 확인(nsmAssertHandle)


def test_assert_handle_classifies_by_screen(clock) -> None:
    err = AsideError("로그인돼 있지 않음", "login", _ev("instagram-20261030-032618"))
    a = InstagramAdapter("@nonsleepmarketer", clock, repl=FakeRepl(error=err))
    with pytest.raises(LoginRequiredError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.kind == "auth" and e.value.auth_state == "verify"


def test_assert_handle_blank_page_is_transient_not_auth(clock) -> None:
    """2026-11-02 23:54 스레드: 정상 글 주소의 하얀 화면은 로그인 풀림이 아니라 덜 열림."""
    err = AsideError("로그인돼 있지 않음", "login", _ev("threads-20261102-235339"))
    a = ThreadsAdapter("@nonsleepmarketer", clock, repl=FakeRepl(error=err))
    with pytest.raises(PlatformError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.kind == "transient" and not isinstance(e.value, LoginRequiredError)


def test_real_logout_with_login_form_is_still_auth(clock) -> None:
    ev = {
        "url": "https://www.threads.com/login",
        "text": "Instagram으로 계속\n로그인",
        "labels": [],
    }
    a = ThreadsAdapter(
        "@nonsleepmarketer",
        clock,
        repl=FakeRepl(error=AsideError("로그인돼 있지 않음", "login", ev)),
    )
    with pytest.raises(LoginRequiredError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.kind == "auth" and e.value.auth_state == "logged_out"


def test_ports_are_unchanged() -> None:
    """HealthStatus·포트는 동결: 세부 상태는 어댑터·오류의 속성으로만 둔다."""
    from nsm.ports import PlatformAdapter

    assert set(HealthStatus.model_fields) == {"logged_in", "detail"}
    assert "auth_state" not in PlatformAdapter.__dict__


# ------------------------------------------------------------------ 리뷰 반영


def test_unread_screen_is_not_blank() -> None:
    """화면 글 읽기가 시간 초과·실패하면(text 키 없음) 빈 화면이 아니라 모르는 것이다."""
    page = {"url": "https://www.instagram.com/", "screenshot": "x"}
    assert not is_blank(page) and classify(page) == "unknown"


def test_sms_two_factor_url_wins_over_code_words() -> None:
    page = {
        "url": "https://www.instagram.com/accounts/login/two_factor?next=%2F",
        "text": "Enter the 6-digit code we sent to +82 •••• 12",
        "labels": [],
    }
    assert classify(page) == "two_factor"


def test_login_word_alone_is_not_a_login_form() -> None:
    """Meta 안내문 어디에나 있는 '로그인'·'password' 낱말만으로는 로그아웃이 아니다."""
    page = {
        "url": "https://www.instagram.com/",
        "text": "Meta는 회원님의 로그인을 돕고 password 보호",
        "labels": [],
    }
    assert classify(page) == "unknown"
    form = {"url": "https://www.instagram.com/", "text": "비밀번호를 잊으셨나요?", "labels": []}
    assert classify(form) == "logged_out"


def test_non_dict_evidence_does_not_crash(clock) -> None:
    a = ThreadsAdapter(
        "@me", clock, repl=FakeRepl(error=AsideError("로그인돼 있지 않음", "login", "garbage"))
    )  # type: ignore[arg-type]
    with pytest.raises(LoginRequiredError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.auth_state is None
    a.repl = FakeRepl({"me": "", "page": ["x"]})
    assert not a.health().logged_in and a.auth_state is None


def test_screenshot_only_when_evidence_can_be_saved(clock) -> None:
    a = InstagramAdapter(
        "@nonsleepmarketer",
        clock,
        repl=FakeRepl({"me": "", "page": _ev("instagram-20261029-085642")}),
    )
    seen: list[bool] = []
    run = a.repl.run
    a.repl.run = lambda body, args=None, files=None: (
        seen.append(args["shot"]),
        run(body, args, files),
    )[1]
    a.health()  # 증거를 남김 → 다음 한 시간은 찍지 않는다
    a.health()
    assert seen == [True, False]
