"""인스타그램 실계정 어댑터: 가짜 실행기로 계약(포트) 동작을 검사한다. 실제 화면은 -m contract."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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
