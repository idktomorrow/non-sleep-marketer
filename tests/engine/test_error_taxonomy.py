"""오류 종류 표(E1, ADR 0054): 5종이 표대로 처리되는지, 실패 증거가 남고 정리되는지, 저장 실패가 원래 오류를 덮지 않는지."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.evidence import prune_evidence, redact, redact_url, save_evidence
from nsm.adapters.platforms.aside_common import (
    LoginRequiredError,
    PlatformError,
    RateLimitedError,
    kind_for_code,
)
from nsm.adapters.platforms.threads import ThreadsAdapter
from nsm.config.store import parse_windows
from nsm.domain.errors import ERROR_KINDS, POLICIES, kind_of, policy_for
from nsm.domain.models import Classification
from nsm.engine.collect import run_collect
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY, run_generate
from nsm.engine.health_metrics import run_health
from nsm.engine.maintenance import run_maintenance
from nsm.engine.publish import PAUSED_KEY, run_publish
from nsm.ports import AdapterError
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakePlatformAdapter,
    FakeRenderer,
    FakeSource,
    FakeTextGenerator,
    load_source_items,
)

# ------------------------------------------------------------------ 표 자체


def test_table_has_five_kinds_and_matches_current_behavior() -> None:
    assert (
        set(ERROR_KINDS)
        == set(POLICIES)
        == {
            "transient",
            "rate_limited",
            "auth",
            "ui_changed",
            "config_or_bug",
        }
    )
    assert POLICIES["rate_limited"].retry == "until" and not POLICIES["rate_limited"].pause_platform
    assert POLICIES["ui_changed"].retry == "none" and POLICIES["ui_changed"].evidence
    for k in ("transient", "auth", "config_or_bug"):
        assert POLICIES[k].retry == "schedule"  # 지금처럼 15·60·240분


def test_kind_of_reads_attribute_retry_at_and_falls_back() -> None:
    from datetime import UTC, datetime

    assert kind_of(PlatformError("x", "ui_changed")) == "ui_changed"
    assert kind_of(LoginRequiredError("x")) == "auth"
    assert kind_of(RateLimitedError("x", datetime.now(UTC))) == "rate_limited"
    assert kind_of(AdapterError("종류 없음")) == "transient"  # 어댑터 밖의 외부 실패
    assert kind_of(KeyError("bug")) == "config_or_bug"


@pytest.mark.parametrize(
    ("code", "message", "kind"),
    [
        ("script", '"새로운 게시물" 메뉴 없음', "ui_changed"),
        ("script", "locator.waitFor: Timeout 20000ms exceeded", "transient"),
        ("script", "file fetch 500", "transient"),
        ("page", "페이지를 열 수 없음", "transient"),
        ("timeout", "Aside 응답 시간 초과", "transient"),
        ("cli", "Aside 결과 줄이 없음", "transient"),
        ("login", "로그인돼 있지 않음", "auth"),
        ("mismatch", "@a ≠ @b", "auth"),
    ],
)
def test_aside_codes_map_to_kinds(code: str, message: str, kind: str) -> None:
    assert kind_for_code(code, message) == kind


# ------------------------------------------------------------------ 엔진: 게시


@pytest.fixture
def ctx(db, clock, app_config, tmp_path) -> JobContext:
    c = JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", load_source_items())],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9)
        ),
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
        renderer=FakeRenderer(),
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram")},
        media_dir=tmp_path / "media",
    )
    c.repo.set_setting(AUTO_PUBLISH_KEY, True)
    c.config = c.config.model_copy(
        update={
            "settings": c.config.settings.model_copy(update={"protect_count": 0}),
            "persona": c.config.persona.model_copy(
                update={"active_hours": parse_windows("00:00-23:59")}
            ),
        }
    )
    run_collect(c)
    run_generate(c)
    return c


class _Failing(FakePlatformAdapter):
    def __init__(self, platform, clock, error: Exception) -> None:
        super().__init__(platform, clock=clock)
        self.error = error
        self.health_error: Exception | None = None

    def publish(self, post):
        raise self.error

    def health(self):
        if self.health_error is not None:
            raise self.health_error
        return super().health()


def _pub(ctx: JobContext):
    return ctx.repo.due_publications("threads", ctx.clock.now())[0]


def _with(ctx: JobContext, error: Exception) -> str:
    ctx.adapters["threads"] = _Failing("threads", ctx.clock, error)
    pid = _pub(ctx).id
    run_publish(ctx)
    return pid


@pytest.mark.parametrize("kind", ["transient", "auth", "config_or_bug"])
def test_schedule_kinds_retry_after_first_delay(ctx: JobContext, kind: str) -> None:
    err = LoginRequiredError("threads 로그인 필요") if kind == "auth" else PlatformError("x", kind)
    pid = _with(ctx, err)
    p = ctx.repo.get_publication(pid)
    first = ctx.config.settings.retry_delays_minutes[0]
    assert p.status == "failed" and p.next_attempt_at == ctx.clock.now() + timedelta(minutes=first)
    assert ctx.repo.get_content(p.content_id).status != "pending_review"


def test_rate_limited_waits_without_spending_attempts(ctx: JobContext) -> None:
    until = ctx.clock.now() + timedelta(hours=2)
    pid = _with(ctx, RateLimitedError("429", until))
    p = ctx.repo.get_publication(pid)
    assert p.status == "failed" and p.next_attempt_at == until and p.attempts == 0
    assert any("요청 제한" in n[0] for n in ctx.notifier.calls)


def test_ui_changed_goes_straight_to_review_with_evidence_path(ctx: JobContext) -> None:
    err = PlatformError('threads: "게시 버튼 없음"', "ui_changed")
    err.evidence_dir = "C:/x/shots/threads-20261027-010203"
    pid = _with(ctx, err)
    p = ctx.repo.get_publication(pid)
    assert p.status == "abandoned" and p.attempts == 1  # 다시 하지 않는다(해도 같다)
    content = ctx.repo.get_content(p.content_id)
    assert content.status == "pending_review" and "화면 변경" in content.review_reason
    title, body, level = ctx.notifier.calls[-1]
    assert "화면 변경" in title and "threads-20261027-010203" in body and level == "error"


# ------------------------------------------------------------------ 엔진: health


def test_health_pauses_on_auth_and_rests_on_rate_limit(ctx: JobContext) -> None:
    a = _Failing("threads", ctx.clock, AdapterError("unused"))
    a.health_error = LoginRequiredError("threads 로그인 필요")
    ctx.adapters = {"threads": a}
    run_health(ctx)
    assert "threads" in ctx.repo.get_setting(PAUSED_KEY, [])
    assert ctx.notifier.calls[-1][0] == "threads 로그인 필요"
    ctx.repo.set_setting(PAUSED_KEY, [])
    a.health_error = RateLimitedError("429", ctx.clock.now() + timedelta(hours=2))
    run_health(ctx)
    assert ctx.repo.get_setting(PAUSED_KEY, []) == []  # 요청 제한은 멈추지 않는다(ADR 0038)


def test_policy_for_is_what_engine_reads() -> None:
    assert policy_for(PlatformError("x", "ui_changed")).label.startswith("외부 화면 변경")


# ------------------------------------------------------------------ 증거


class _Repl:
    def __init__(self, error: AsideError) -> None:
        self.error = error

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        raise self.error


_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_adapter_saves_evidence_and_appends_folder(clock, tmp_path, _isolated_evidence) -> None:
    ev = {
        "url": "https://www.threads.com/post?x=1&access_token=SECRET123",
        "dialogs": 2,
        "text": "게시 sessionid=abcdef 확인",
        "labels": ["닫기", "Authorization: Bearer xyz"],
        "screenshot": _PNG,
    }
    a = ThreadsAdapter("@me", clock, repl=_Repl(AsideError("게시 버튼 없음", "script", ev)))
    with pytest.raises(PlatformError) as e:
        a.health()
    err = e.value
    assert err.kind == "ui_changed" and err.evidence_dir and "(증거: " in str(err)
    d = Path(err.evidence_dir)
    assert d.parent == _isolated_evidence and d.name.startswith("threads-")
    assert (d / "screenshot.png").read_bytes().startswith(b"\x89PNG")
    info = json.loads((d / "dialogs.json").read_text("utf-8"))
    assert info["dialogs"] == 2 and info["kind"] == "ui_changed"
    dump = (d / "dialogs.json").read_text("utf-8")
    assert "SECRET123" not in dump and "abcdef" not in dump and "xyz" not in dump
    assert (d / "log-tail.txt").exists()


def test_rate_limit_and_interrupt_leave_no_evidence(clock, _isolated_evidence) -> None:
    for code in ("ratelimit", "interrupted"):
        a = ThreadsAdapter("@me", clock, repl=_Repl(AsideError("x", code, {"url": "u"})))
        with pytest.raises(PlatformError):
            a.health()
    assert not _isolated_evidence.exists() or not any(_isolated_evidence.iterdir())


def test_evidence_save_failure_keeps_original_error(clock, monkeypatch, tmp_path) -> None:
    from nsm.adapters import evidence

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # 파일이라 그 아래 폴더를 만들 수 없다
    monkeypatch.setattr(evidence, "shots_dir", lambda: blocker / "shots")
    a = ThreadsAdapter("@me", clock, repl=_Repl(AsideError("게시 버튼 없음", "script", {})))
    with pytest.raises(PlatformError) as e:
        a.health()
    assert e.value.kind == "ui_changed" and e.value.evidence_dir is None
    assert str(e.value) == "threads: 게시 버튼 없음"  # 문구가 그대로
    assert save_evidence("threads", clock.now(), {}, root=blocker / "shots") is None


def test_redaction_helpers() -> None:
    assert "<지움>" in redact("token=abc") and "abc" not in redact("token=abc")
    assert "sk-ant-" not in redact("key sk-ant-ABCDEFGHIJKLMNOP")
    assert "AIza" not in redact("AIzaSyA1234567890abcdefghijk")
    assert "SECRET" not in redact_url("https://x.com/a?access_token=SECRET&page=2")
    assert "page=2" in redact_url("https://x.com/a?access_token=SECRET&page=2")


def test_maintenance_prunes_old_evidence(db, clock, app_config, _isolated_evidence) -> None:
    now = clock.now().astimezone()
    old = _isolated_evidence / f"threads-{(now - timedelta(days=20)):%Y%m%d-%H%M%S}"
    new = _isolated_evidence / f"instagram-{(now - timedelta(days=3)):%Y%m%d-%H%M%S}"
    other = _isolated_evidence / "keep-me"
    for d in (old, new, other):
        d.mkdir(parents=True)
    ctx = JobContext(
        config=app_config, repo=Repository(db, clock), clock=clock, notifier=FakeNotifier()
    )
    r = run_maintenance(ctx)
    assert r.counts["evidence_pruned"] == 1 and not old.exists() and new.exists() and other.exists()
    assert "실패 증거 1개 지움" in r.summary()
    assert prune_evidence(clock.now(), 14, root=_isolated_evidence / "missing") == 0
