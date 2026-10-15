"""인스타그램 실계정 어댑터: 가짜 실행기로 계약(포트) 동작을 검사한다. 실제 화면은 -m contract."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.platforms.aside_common import AccountMismatchError, LoginRequiredError
from nsm.adapters.platforms.instagram import InstagramAdapter, _probe
from nsm.domain.labels import VIRTUAL_PERSON_TEXT
from nsm.domain.models import Comment, RenderedPost
from nsm.ports import AdapterError, NotSupportedError, PlatformAdapter


class FakeRepl:
    def __init__(self, data=None, error: AsideError | None = None) -> None:
        self.data, self.error = data, error
        self.calls: list[tuple[str, dict, list]] = []

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        self.calls.append((body, args or {}, list(files or [])))
        if self.error:
            raise self.error
        return self.data


def _adapter(tmp_path: Path, clock, data=None, error=None, handle="@nonsleepmarketer"):
    media = tmp_path / "media"
    (media / "cards" / "c1").mkdir(parents=True)
    return InstagramAdapter(handle, clock, repl=FakeRepl(data, error), media_dir=media), media


def _post(media: Path, images: bool = True) -> RenderedPost:
    img = media / "cards" / "c1" / "slide-01.png"
    img.write_bytes(b"png")
    text = f"{VIRTUAL_PERSON_TEXT}\n\n가을 원두 고르는 법 다섯 가지\n#홈카페"
    return RenderedPost(
        content_id="c1", platform="instagram", text=text, images=[img] if images else []
    )


def test_is_a_platform_adapter_without_hide(tmp_path, clock) -> None:
    a, _ = _adapter(tmp_path, clock)
    assert isinstance(a, PlatformAdapter)
    assert "hide_comment" not in a.capabilities()  # 웹에 숨기기가 없다(ADR 0018)
    with pytest.raises(NotSupportedError):
        a.hide_comment(_comment())


def test_publish_returns_post_url_and_passes_values_as_data(tmp_path, clock) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "Cabc", "ratio": 0.8})
    res = a.publish(_post(media))
    assert (res.external_id, res.url) == ("Cabc", "https://www.instagram.com/p/Cabc/")
    body, args, files = a.repl.calls[0]
    assert "4:5" in body  # 자르기는 반드시 4:5
    assert "Threads" in body  # 스레드 동시 공유 스위치를 끄는 단계가 있다
    assert args["probe"] == "가을 원두 고르는 법 다섯 가지" and "원두" not in body
    assert files[0].name == "slide-01.png"


def test_publish_needs_an_image(tmp_path, clock) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "X"})
    with pytest.raises(AdapterError):
        a.publish(_post(media, images=False))
    assert a.repl.calls == []


def test_wrong_ratio_is_reported_loudly(tmp_path, clock, caplog) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "Cabc", "ratio": 1.0})
    with caplog.at_level(logging.ERROR):
        a.publish(_post(media))
    assert "4:5가 아님" in caplog.text


def test_posted_but_unconfirmed_is_success_without_id(tmp_path, clock) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "", "posted": "unknown"})
    res = a.publish(_post(media))
    assert res.external_id == "" and res.url == "https://www.instagram.com/nonsleepmarketer/"


@pytest.mark.parametrize(
    ("code", "exc"),
    [("login", LoginRequiredError), ("mismatch", AccountMismatchError), ("script", AdapterError)],
)
def test_errors_map(tmp_path, clock, code, exc) -> None:
    a, media = _adapter(tmp_path, clock, error=AsideError("x", code=code))
    with pytest.raises(exc):
        a.publish(_post(media))


def test_fetch_comments_skips_own_empty_and_old(tmp_path, clock) -> None:
    rows = [
        {
            "handle": "fan",
            "cid": "111",
            "text": "안녕하세요",
            "at": "2026-10-11T10:00:00.000Z",
            "parent": "P1",
        },
        {
            "handle": "nonsleepmarketer",
            "cid": "112",
            "text": "내 답글",
            "at": "2026-10-11T10:01:00.000Z",
            "parent": "P1",
        },
        {
            "handle": "fan2",
            "cid": "113",
            "text": "",
            "at": "2026-10-11T10:02:00.000Z",
            "parent": "P1",
        },
        {
            "handle": "old",
            "cid": "100",
            "text": "옛 댓글",
            "at": "2026-10-10T10:00:00.000Z",
            "parent": "P1",
        },
    ]
    a, _ = _adapter(tmp_path, clock, data=rows)
    got = a.fetch_new_comments(["P1"], datetime(2026, 10, 11, tzinfo=UTC))
    assert [c.external_id for c in got] == ["fan:111"]
    assert "/" not in got[0].external_id  # 대시보드 경로(/comments/{key}/reply)에 들어간다


def _comment() -> Comment:
    return Comment(
        platform="instagram",
        external_id="fan:111",
        post_external_id="P1",
        author_id="fan",
        author_name="fan",
        text="안녕하세요",
        created_at=datetime(2026, 10, 11, 10, tzinfo=UTC),
    )


def test_reply_targets_comment_by_id(tmp_path, clock) -> None:
    a, _ = _adapter(tmp_path, clock, data={"ok": True})
    assert a.reply(_comment(), "고마워요") == ""
    _, args, _ = a.repl.calls[-1]
    assert (args["parent"], args["author"], args["cid"], args["text"]) == (
        "P1",
        "fan",
        "111",
        "고마워요",
    )


def test_metrics_mapping(tmp_path, clock) -> None:
    data = {"views": 40, "reach": None, "likes": 3, "comments": 2, "saves": 1, "shares": 0}
    a, _ = _adapter(tmp_path, clock, data=data)
    m = a.read_post_metrics("P1")
    assert (m.views, m.likes, m.comments, m.saves, m.shares) == (40, 3, 2, 1, 0)
    a.repl.data = {"followers": 12}
    assert a.read_account_metrics().followers == 12


def test_probe_rules() -> None:
    assert _probe(f"{VIRTUAL_PERSON_TEXT}\n\n#태그만") == ""


def test_runtime_builds_both_live_adapters(app_config, nsm_home) -> None:
    from nsm import runtime
    from nsm.domain.clock import SystemClock

    acc = {"threads": "@nonsleepmarketer", "instagram": "@nonsleepmarketer"}
    s = app_config.settings.model_copy(update={"dry_run": False, "accounts": acc})
    got = runtime.make_platform_adapters(
        app_config.model_copy(update={"settings": s}), SystemClock()
    )
    assert set(got) == {"threads", "instagram"} and isinstance(got["instagram"], InstagramAdapter)


def test_publish_waits_while_buttons_are_disabled(tmp_path, clock) -> None:
    """2026-10-14: 편집 단계의 "다음"이 처리 중(비활성)일 때 눌러 "Element is disabled"로 실패했다.
    비활성 버튼은 누르지 않고 기다리며, 무시된 클릭은 다시 누른다."""
    a, media = _adapter(tmp_path, clock, data={"code": "Cabc", "ratio": 0.8})
    a.publish(_post(media))
    body = a.repl.calls[0][0]
    assert "nsmDisabled" in body and "'busy'" in body
    assert "=== true, 20000" in body  # 공유하기는 실제로 눌렀을 때만 넘어간다


# --- 요청 제한(HTTP 429), ADR 0038 -------------------------------------------------


def test_rate_limit_page_is_not_reported_as_logged_out(tmp_path, clock) -> None:
    """2026-10-14~15: 429 화면에는 내 프로필 사진이 없어 "로그인돼 있지 않음"으로 잘못 보고했다.
    로그인 확인 전에 페이지가 막혔는지 먼저 본다."""
    a, _ = _adapter(tmp_path, clock, data=[])
    a.fetch_new_comments(["P1"], None)
    body = a.repl.calls[0][0]
    assert body.index("await nsmCheckPage(pg);") < body.index("const me = ((await readHandle")
    assert "HTTP ERROR 429" in body and "'ratelimit'" in body
    a.repl.data = {"me": "nonsleepmarketer"}
    a.health()
    health = a.repl.calls[-1][0]
    assert health.index("await nsmCheckPage(pg);") < health.index("return { me:")


@pytest.mark.parametrize(
    "error",
    [
        AsideError("요청이 너무 많음(HTTP 429)", code="ratelimit"),
        AsideError("page.goto: net::ERR_FAILED (HTTP ERROR 429)"),
    ],
    ids=["429-page", "429-in-message"],
)
def test_rate_limit_rests_without_opening_the_browser(tmp_path, clock, error) -> None:
    from nsm.adapters.platforms.aside_common import RateLimitedError

    a, _ = _adapter(tmp_path, clock, error=error)
    with pytest.raises(RateLimitedError) as e:
        a.fetch_new_comments(["P1"], None)
    assert e.value.retry_at == clock.now() + timedelta(hours=2) == a.blocked_until
    assert not isinstance(e.value, LoginRequiredError)
    calls = len(a.repl.calls)
    a.repl.error = None
    a.repl.data = {"me": "nonsleepmarketer"}
    for call in (lambda: a.fetch_new_comments(["P1"], None), a.health, a.read_account_metrics):
        with pytest.raises(RateLimitedError):
            call()
    assert len(a.repl.calls) == calls  # 쉬는 동안 브라우저 요청 0건
    clock.advance(hours=2)
    assert a.health().logged_in is True  # 쉬고 나면 다시


def test_rate_limit_backs_off_longer_when_hit_again(tmp_path, clock) -> None:
    from nsm.adapters.platforms.aside_common import RateLimitedError

    a, _ = _adapter(tmp_path, clock, error=AsideError("429", code="ratelimit"))
    waits = []
    for _ in range(4):
        with pytest.raises(RateLimitedError) as e:
            a.read_account_metrics()
        waits.append(e.value.retry_at - clock.now())
        clock.set(e.value.retry_at)
    assert waits == [timedelta(hours=h) for h in (2, 4, 8, 8)]  # 두 배씩, 최대 8시간
    a.repl.error, a.repl.data = None, {"followers": 1}
    a.read_account_metrics()  # 성공하면 처음(2시간)으로
    a.repl.error = AsideError("429", code="ratelimit")
    with pytest.raises(RateLimitedError) as e:
        a.read_account_metrics()
    assert e.value.retry_at - clock.now() == timedelta(hours=2)


@pytest.mark.parametrize(
    "error",
    [
        AsideError("페이지를 열 수 없음(ERR_INTERNET_DISCONNECTED)", code="page"),
        # 404(지운 글)·500에도 나오는 오류라 요청 제한으로 보지 않는다(쉬면 몇 시간 멈춤)
        AsideError(
            "page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://www.instagram.com/p/X/"
        ),
    ],
    ids=["network", "http-error"],
)
def test_unopenable_page_is_a_plain_error_not_login(tmp_path, clock, error) -> None:
    a, _ = _adapter(tmp_path, clock, error=error)
    with pytest.raises(AdapterError) as e:
        a.read_account_metrics()
    assert not isinstance(e.value, LoginRequiredError) and a.blocked_until is None


class SeqRepl(FakeRepl):
    """호출마다 다음 결과(값 또는 예외)."""

    def __init__(self, results) -> None:
        super().__init__()
        self.results = list(results)

    def run(self, body, args=None, files=None):
        self.calls.append((body, args or {}, list(files or [])))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_every_comment_page_is_checked_for_rate_limit(tmp_path, clock) -> None:
    """둘째 글부터 429여도 알아본다(핸들 확인은 첫 글만)."""
    a, _ = _adapter(tmp_path, clock, data=[])
    a.fetch_new_comments(["P1"], None)
    assert "else { await nsmCheckPage(pg); }" in a.repl.calls[0][0]


def test_rate_limit_in_a_later_batch_is_not_swallowed(tmp_path, clock) -> None:
    """앞 묶음은 읽었고 뒤 묶음이 막힘: 부분 결과를 돌려주면 since가 앞당겨져 못 읽은 글의 댓글을 잃는다."""
    from nsm.adapters.platforms.aside_common import RateLimitedError
    from nsm.adapters.platforms.instagram import COMMENT_BATCH

    a, _ = _adapter(tmp_path, clock)
    row = {"handle": "fan", "cid": "1", "text": "hi", "at": "2026-10-11T00:00:00Z", "parent": "P1"}
    a.repl = SeqRepl([[row], AsideError("429", code="ratelimit")])
    with pytest.raises(RateLimitedError):
        a.fetch_new_comments([f"P{i}" for i in range(COMMENT_BATCH + 1)], None)


def test_login_check_success_does_not_reset_backoff(tmp_path, clock) -> None:
    """홈 화면(로그인 점검)은 글 페이지가 막혀도 열린다. 그 성공으로 백오프가 2시간으로 돌아가면 안 된다."""
    from nsm.adapters.platforms.aside_common import RateLimitedError

    a, _ = _adapter(tmp_path, clock)
    limit = AsideError("429", code="ratelimit")
    a.repl = SeqRepl([limit, {"me": "nonsleepmarketer"}, limit])
    with pytest.raises(RateLimitedError) as e:
        a.read_account_metrics()
    clock.set(e.value.retry_at)
    assert a.health().logged_in
    with pytest.raises(RateLimitedError) as e:
        a.read_account_metrics()
    assert e.value.retry_at - clock.now() == timedelta(hours=4)
