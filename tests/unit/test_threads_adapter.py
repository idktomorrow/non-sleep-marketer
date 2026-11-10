"""스레드 실계정 어댑터: Aside 없이 가짜 실행기로 계약(포트) 동작을 검사한다. 실제 화면은 -m contract."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.platforms.aside_common import (
    AccountMismatchError,
    LoginRequiredError,
    PlatformError,
    check_upload_paths,
)
from nsm.adapters.platforms.threads import ThreadsAdapter, _probe
from nsm.domain.labels import VIRTUAL_PERSON_TEXT
from nsm.domain.models import Comment, RenderedPost
from nsm.ports import AdapterError, PlatformAdapter


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


def _adapter(tmp_path: Path, clock, data=None, error=None, handle="@NonSleepMarketer"):
    media = tmp_path / "media"
    (media / "cards" / "c1").mkdir(parents=True)
    return ThreadsAdapter(handle, clock, repl=FakeRepl(data, error), media_dir=media), media


def _post(media: Path, text: str | None = None) -> RenderedPost:
    img = media / "cards" / "c1" / "slide-01.png"
    img.write_bytes(b"png")
    body = text or f"{VIRTUAL_PERSON_TEXT}\n\n핸드드립 물 온도 정리\n#핸드드립"
    return RenderedPost(content_id="c1", platform="threads", text=body, images=[img])


def test_is_a_platform_adapter(tmp_path, clock) -> None:
    a, _ = _adapter(tmp_path, clock)
    assert isinstance(a, PlatformAdapter) and "hide_comment" in a.capabilities()


def test_publish_passes_text_as_data_and_returns_url(tmp_path, clock) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "ABC123"})
    res = a.publish(_post(media))
    assert res.external_id == "ABC123"
    assert res.url == "https://www.threads.com/@nonsleepmarketer/post/ABC123"
    body, args, files = a.repl.calls[0]
    assert args["handle"] == "nonsleepmarketer"  # 게시 직전 로그인 계정 확인에 쓰인다
    assert args["text"].startswith(VIRTUAL_PERSON_TEXT) and "핸드드립" not in body
    assert args["probe"] == "핸드드립 물 온도 정리" and files[0].name == "slide-01.png"
    assert args["since"] < datetime.now(UTC).isoformat()  # 멱등 확인은 최근 글만


def test_probe_skips_label_and_hashtags() -> None:
    text = f"{VIRTUAL_PERSON_TEXT}\n\n첫 줄 본문\n  #들여쓴 태그\n둘째 줄 본문입니다\n#태그"
    assert _probe(text) == "첫 줄 본문 둘째 줄 본문입니다"


def test_probe_is_empty_when_body_too_short_or_only_hashtags() -> None:
    # 표기 줄로 돌아가면 모든 기존 글과 맞아 새 글이 안 올라간다
    assert _probe(f"{VIRTUAL_PERSON_TEXT}\n\n#태그 #또") == ""
    assert _probe(f"{VIRTUAL_PERSON_TEXT}\n\n좋아요") == ""


def test_only_png_inside_media_can_be_uploaded(tmp_path: Path) -> None:
    media = tmp_path / "media"
    (media / "cards").mkdir(parents=True)
    ok = media / "cards" / "a.png"
    ok.write_bytes(b"x")
    outside = tmp_path / "config.png"
    outside.write_bytes(b"x")
    txt = media / "cards" / "a.txt"
    txt.write_text("x")
    assert check_upload_paths([ok], media) == [ok.resolve()]
    for bad in (outside, txt, media / "cards" / ".." / ".." / "config.png", media / "missing.png"):
        with pytest.raises(AdapterError):
            check_upload_paths([bad], media)


def test_no_account_configured_means_no_live_action(tmp_path, clock) -> None:
    a, media = _adapter(tmp_path, clock, data={"code": "X"}, handle=None)
    with pytest.raises(AccountMismatchError):
        a.publish(_post(media))
    assert a.repl.calls == []


@pytest.mark.parametrize(
    ("code", "exc"),
    [("login", LoginRequiredError), ("mismatch", AccountMismatchError), ("script", AdapterError)],
)
def test_errors_map_to_adapter_errors(tmp_path, clock, code, exc) -> None:
    a, media = _adapter(tmp_path, clock, error=AsideError("x", code=code))
    with pytest.raises(exc):
        a.publish(_post(media))


def test_fetch_comments_skips_own_and_old(tmp_path, clock) -> None:
    rows = [
        {
            "code": "C1",
            "handle": "fan",
            "text": "안녕하세요",
            "at": "2026-10-11T10:00:00.000Z",
            "parent": "P1",
        },
        {
            "code": "C2",
            "handle": "nonsleepmarketer",
            "text": "내 답글",
            "at": "2026-10-11T10:05:00.000Z",
            "parent": "P1",
        },
        {
            "code": "C0",
            "handle": "old",
            "text": "옛 댓글",
            "at": "2026-10-10T10:00:00.000Z",
            "parent": "P1",
        },
    ]
    a, _ = _adapter(tmp_path, clock, data=rows)
    got = a.fetch_new_comments(["P1"], datetime(2026, 10, 11, tzinfo=UTC))
    assert [(c.external_id, c.post_external_id, c.author_id) for c in got] == [
        ("fan:C1", "P1", "fan")
    ]
    assert a.fetch_new_comments([], None) == []


def _comment() -> Comment:
    return Comment(
        platform="threads",
        external_id="fan:C1",
        post_external_id="P1",
        author_id="fan",
        author_name="fan",
        text="안녕하세요",
        created_at=datetime(2026, 10, 11, 10, tzinfo=UTC),
    )


def test_reply_and_hide_target_the_comment(tmp_path, clock) -> None:
    a, _ = _adapter(tmp_path, clock, data={"code": "R1"})
    assert a.reply(_comment(), "고마워요") == "R1"
    _, args, _ = a.repl.calls[-1]
    assert (args["author"], args["code"], args["parent"], args["text"]) == (
        "fan",
        "C1",
        "P1",
        "고마워요",
    )
    a.hide_comment(_comment())
    _, args, _ = a.repl.calls[-1]
    assert (args["parent"], args["code"]) == ("P1", "C1")
    bad = _comment().model_copy(update={"external_id": "noslash"})
    with pytest.raises(AdapterError):
        a.reply(bad, "x")


def test_metrics_mapping(tmp_path, clock) -> None:
    a, _ = _adapter(
        tmp_path, clock, data={"views": 30, "reach": 12, "likes": 2, "comments": 1, "shares": 0}
    )
    m = a.read_post_metrics("P1")
    assert (m.views, m.reach, m.likes, m.comments, m.shares, m.saves) == (30, 12, 2, 1, 0, None)
    a.repl.data = {"followers": 7}
    assert a.read_account_metrics().followers == 7


@pytest.mark.parametrize(
    ("me", "ok"), [("nonsleepmarketer", True), ("", False), ("someoneelse", False)]
)
def test_health_checks_login_and_account(tmp_path, clock, me, ok) -> None:
    a, _ = _adapter(tmp_path, clock, data={"me": me})
    assert a.health().logged_in is ok


def test_runtime_builds_live_adapter_only_with_account(monkeypatch, app_config, nsm_home) -> None:
    from nsm import runtime
    from nsm.domain.clock import SystemClock

    s = app_config.settings.model_copy(update={"dry_run": False, "accounts": {}})
    cfg = app_config.model_copy(update={"settings": s})
    assert runtime.make_platform_adapters(cfg, SystemClock()) == {}
    s2 = s.model_copy(update={"accounts": {"threads": "@nonsleepmarketer"}})
    got = runtime.make_platform_adapters(cfg.model_copy(update={"settings": s2}), SystemClock())
    assert list(got) == ["threads"] and isinstance(got["threads"], ThreadsAdapter)


def test_comment_key_fits_dashboard_route(tmp_path, clock, db) -> None:
    """대시보드는 "플랫폼:ID"를 /comments/{key}/reply 경로에 넣으므로 ID에 "/"가 없어야 한다."""
    rows = [
        {
            "code": "C1",
            "handle": "fan",
            "text": "hi",
            "at": "2026-10-11T10:00:00.000Z",
            "parent": "P1",
        }
    ]
    a, _ = _adapter(tmp_path, clock, data=rows)
    c = a.fetch_new_comments(["P1"], None)[0]
    assert "/" not in c.external_id


def test_publish_is_idempotent_existing_post_is_not_posted_again(tmp_path, clock) -> None:  # (4)
    """지난 시도가 이미 올라갔으면(게시 전 멱등 확인) 그 글을 돌려준다. 스크립트는 게시 단계에 가지 않는다."""
    a, media = _adapter(tmp_path, clock, data={"code": "OLD1", "existing": True})
    res = a.publish(_post(media))
    assert res.external_id == "OLD1" and res.url.endswith("/post/OLD1")
    body, args, _ = a.repl.calls[0]
    # 멱등 확인이 게시 버튼보다 먼저다
    assert body.index("if (before) return") < body.index("nsmClickText(dlg, '게시')")
    # 재시도는 게시 시간·절전·쉬기로 밤을 넘긴다(11:30 실패 → 다음 날 09:00 재시도도 찾게)
    assert datetime.fromisoformat(args["since"]) <= clock.now() - timedelta(hours=48)


@pytest.mark.parametrize("posted", [True, "unknown"])  # (1) (2)
def test_posted_but_not_found_is_a_transient_error_with_evidence(
    tmp_path, clock, posted, _isolated_evidence
) -> None:
    """C43: 프로필에서 글을 못 찾으면 성공이 아니라 일시적 실패(엔진이 재시도, 재시도는 멱등 확인부터)."""
    page = {"url": "https://www.threads.com/@nonsleepmarketer", "dialogs": 0, "text": "프로필"}
    a, media = _adapter(tmp_path, clock, data={"code": "", "posted": posted, "page": page})
    with pytest.raises(PlatformError) as ei:
        a.publish(_post(media))
    err = ei.value
    assert err.kind == "transient" and "게시를 확인하지 못함" in str(err)
    assert err.evidence_dir and Path(err.evidence_dir).is_dir()
    assert str(err).endswith(f"(증거: {Path(err.evidence_dir).name})")
    info = json.loads((Path(err.evidence_dir) / "dialogs.json").read_text("utf-8"))
    assert info["kind"] == "publish_unverified" and info["captured"]
    assert a.repl.calls[0][1]["find_ms"] == 90_000  # 게시 뒤 글 찾기 45초 → 90초


def test_short_post_without_probe_keeps_old_success(tmp_path, clock, caplog) -> None:  # (3)
    """본문 8자 미만은 고유 문구가 없어 재시도의 중복을 막을 수 없다: 예전처럼 주소 없이 성공."""
    a, media = _adapter(tmp_path, clock, data={"code": "", "posted": True})
    with caplog.at_level(logging.WARNING):
        res = a.publish(_post(media, f"{VIRTUAL_PERSON_TEXT}\n\n짧은 글\n#태그"))
    assert a.repl.calls[0][1]["probe"] == ""
    assert res.external_id == "" and res.url == "https://www.threads.com/@nonsleepmarketer"
    assert "짧은 글이라 확인 생략" in caplog.text


def test_post_metrics_read_counts_in_one_evaluate(tmp_path, clock) -> None:
    """C43: 지표는 표시(data-nsm-mark)를 달고 다시 찾지 않고 evaluate 한 번 안에서 읽는다(화면이 다시 그려져도)."""
    from nsm.adapters.platforms.threads import _POST_METRICS

    tail = _POST_METRICS.split("const readCounts")[1]
    assert "nsmMarkIcon" not in tail and "data-nsm-mark" not in tail
    assert "closest('[role=\"button\"], button, a')" in tail


def test_comments_are_read_in_small_batches(tmp_path, clock) -> None:
    from nsm.adapters.platforms import threads as T

    a, _ = _adapter(tmp_path, clock, data=[])
    a.fetch_new_comments([f"P{i}" for i in range(T.COMMENT_BATCH * 2 + 1)], None)
    assert [len(args["codes"]) for _, args, _ in a.repl.calls] == [
        T.COMMENT_BATCH,
        T.COMMENT_BATCH,
        1,
    ]


def test_one_failed_batch_does_not_lose_the_others(tmp_path, clock) -> None:
    rows = [
        {
            "code": "C1",
            "handle": "fan",
            "text": "hi",
            "at": "2026-10-11T10:00:00.000Z",
            "parent": "P1",
        }
    ]
    a, _ = _adapter(tmp_path, clock)

    calls = {"n": 0}

    def run(body, args=None, files=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AsideError("시간 초과", code="timeout")
        return rows

    a.repl.run = run
    got = a.fetch_new_comments([f"P{i}" for i in range(8)], None)
    assert [c.external_id for c in got] == ["fan:C1"]


def test_reply_unconfirmed_returns_empty_not_error(tmp_path, clock) -> None:
    a, _ = _adapter(tmp_path, clock, data={"code": ""})
    assert a.reply(_comment(), "고마워요") == ""


def test_find_aside_cli_ignores_cmd_wrappers(tmp_path, monkeypatch) -> None:
    from nsm.adapters import drivers

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    monkeypatch.setattr(drivers.shutil, "which", lambda _: str(tmp_path / "aside.cmd"))
    assert drivers.find_aside_cli() is None
