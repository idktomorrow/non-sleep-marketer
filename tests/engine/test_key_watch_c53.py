"""C53: API 키 만료일 미리 알림, health의 하루 한 번 자동 확인, 생성 중 401/403 즉시 알림.

가짜 notifier·가짜 check_key·가짜 시계만 쓴다. 진짜 키·네트워크 없음.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from nsm.config.secrets import MemorySecretStore
from nsm.engine import key_watch as kw
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import run_health
from nsm.engine.summary import describe
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakePlatformAdapter

CLAUDE = "fake-claude-key-AAAAAAAAAAAAAAAAAAAAAAAA"  # 가짜 값
GEMINI = "fake-gemini-key-CCCCCCCCCCCCCCCCCCCCCC"


class Checker:
    def __init__(self, code: str = "ok", status: int | None = 200) -> None:
        self.code, self.status = code, status
        self.calls: list[str] = []

    def __call__(self, name: str, value: str | None):
        self.calls.append(name)
        return self.code, self.status


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={"threads": FakePlatformAdapter("threads", clock=clock)},
        secrets=MemorySecretStore({"anthropic_api_key": CLAUDE}),
        check_key=Checker(),
    )


def _today(ctx) -> date:
    return kw.today(ctx.clock.now(), ctx.tz)


def _titles(ctx) -> list[str]:
    return [t for t, _, _ in ctx.notifier.calls]


# ------------------------------------------------------------------ 만료일 알림


def test_expiry_alerts_on_7_3_1_0_then_daily(ctx) -> None:
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=8))
    seen: list[tuple[int, list]] = []
    for left in range(8, -4, -1):  # 8일 남음 → 3일 지남
        before = len(ctx.notifier.calls)
        kw.check_keys(ctx)
        kw.check_keys(ctx)  # 같은 날 두 번째 점검은 다시 알리지 않는다
        new = ctx.notifier.calls[before:]
        seen.append((left, [(t, lv) for t, _, lv in new]))
        ctx.clock.advance(days=1)
    got = {left: n for left, n in seen if n}
    assert got == {
        7: [("Claude 키 만료 7일 전", "warn")],
        3: [("Claude 키 만료 3일 전", "warn")],
        1: [("Claude 키 만료 1일 전", "warn")],
        0: [("Claude 키가 오늘 만료됩니다", "error")],
        -1: [("Claude 키가 만료됐습니다", "error")],
        -2: [("Claude 키가 만료됐습니다", "error")],
        -3: [("Claude 키가 만료됐습니다", "error")],
    }
    body = ctx.notifier.calls[0][1]
    assert "설정 → API 키에서 새 키를 저장하세요(재시작 불필요)" in body


def test_missed_day_still_alerts_that_stage_once(ctx) -> None:
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=5))
    kw.check_keys(ctx)  # 7일 전 날은 꺼져 있었다: 5일 남은 날 한 번
    ctx.clock.advance(days=1)
    kw.check_keys(ctx)
    assert _titles(ctx) == ["Claude 키 만료 5일 전"]


def test_changing_date_restarts_stages(ctx) -> None:
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=3))
    kw.check_keys(ctx)
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=7))
    kw.check_keys(ctx)
    assert _titles(ctx) == ["Claude 키 만료 3일 전", "Claude 키 만료 7일 전"]
    st = ctx.repo.get_setting(kw.NOTIFIED_KEY)["anthropic_api_key"]
    assert st["stage"] == 7 and st["date"] == (_today(ctx) + timedelta(days=7)).isoformat()


def test_no_alert_without_key_or_far_date(ctx) -> None:
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=30))
    kw.set_expiry(ctx.repo, "openai_api_key", _today(ctx))  # 키가 없는 메모
    kw.check_keys(ctx)
    assert _titles(ctx) == []


# ------------------------------------------------------------------ 자동 확인


def test_auto_check_once_per_24h_and_ok_is_silent(ctx) -> None:
    check = ctx.check_key
    kw.check_keys(ctx)
    kw.check_keys(ctx)
    ctx.clock.advance(hours=23)
    kw.check_keys(ctx)
    assert check.calls == ["anthropic_api_key"] and _titles(ctx) == []
    ctx.clock.advance(hours=1)
    kw.check_keys(ctx)
    assert check.calls == ["anthropic_api_key"] * 2
    st = ctx.repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]
    assert st["code"] == "ok" and st["at"] == ctx.clock.now().isoformat()


def test_invalid_alerts_once_a_day_and_ok_clears(ctx) -> None:
    ctx.check_key = Checker("invalid", 401)
    kw.check_keys(ctx)
    assert ctx.notifier.calls == [
        (
            "Claude 키가 틀렸거나 만료됨",
            "하루 한 번 자동 확인에서 거절됐습니다. 설정 → API 키에서 새 키를 저장하세요(재시작 불필요)",
            "error",
        )
    ]
    ctx.clock.advance(hours=24)
    kw.check_keys(ctx)
    assert _titles(ctx).count("Claude 키가 틀렸거나 만료됨") == 2  # 다음 날 다시 한 번
    ctx.check_key = Checker("ok")
    ctx.clock.advance(hours=24)
    kw.check_keys(ctx)
    st = ctx.repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]
    assert st["code"] == "ok" and "alerted" not in st and len(ctx.notifier.calls) == 2


def test_limit_warns_once_network_never(ctx) -> None:
    ctx.check_key = Checker("limit", 429)
    kw.check_keys(ctx)
    ctx.clock.advance(hours=25)
    kw.check_keys(ctx)
    assert [(t, lv) for t, _, lv in ctx.notifier.calls] == [
        ("Claude 키 한도·결제 확인 필요", "warn")
    ]
    ctx.check_key = Checker("network", None)
    ctx.clock.advance(hours=25)
    kw.check_keys(ctx)
    assert len(ctx.notifier.calls) == 1
    assert ctx.repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]["code"] == "network"


def test_auto_check_skipped_while_shutting_down(ctx) -> None:
    ctx.shutting_down = lambda: True
    kw.check_keys(ctx)
    assert ctx.check_key.calls == [] and ctx.repo.get_setting(kw.CHECK_KEY, {}) == {}


def test_health_runs_key_step_separately(ctx) -> None:
    ctx.check_key = Checker("invalid", 401)
    r = run_health(ctx)
    assert r.counts["key_alerts"] == ["Claude 키가 틀렸거나 만료됨"]
    assert "API 키 알림: Claude 키가 틀렸거나 만료됨" in describe("health", counts=r.counts)
    assert r.counts["paused"] == []  # 플랫폼 로그인 점검과 섞이지 않는다


def test_key_step_failure_does_not_break_health(ctx) -> None:
    def boom(name, value):
        raise RuntimeError("뜻밖")

    class BrokenSecrets(MemorySecretStore):
        def get(self, key):
            raise RuntimeError("keyring 잠김")

    ctx.check_key = boom
    r = run_health(ctx)  # 확인 함수가 터져도 health는 끝난다
    assert r.counts["paused"] == []
    ctx.secrets = BrokenSecrets()
    r = run_health(ctx)
    assert "key_alerts" not in r.counts


def test_health_without_secrets_skips(ctx) -> None:
    ctx.secrets = None
    r = run_health(ctx)
    assert "key_alerts" not in r.counts and ctx.check_key.calls == []


# ------------------------------------------------------------------ 실행 중 401/403


def _auth_error(status: int = 401):
    import anthropic

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx.Response(status, request=req), body=None
    )


def test_generate_401_alerts_once_a_day_without_health_duplicate(db, clock, app_config) -> None:
    from nsm.domain.models import Classification
    from nsm.engine.collect import run_collect
    from nsm.engine.generate import run_generate
    from tests.fakes import FakeClassifier, FakeSource, FakeTextGenerator, load_source_items

    class Rejecting(FakeTextGenerator):
        def generate_content(self, *a, **k):
            raise _auth_error()

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
        text_generator=Rejecting(),
        secrets=MemorySecretStore({"anthropic_api_key": CLAUDE}),
        check_key=Checker("invalid", 401),
    )
    run_collect(c)
    r = run_generate(c)
    assert any("키가 맞지 않거나" in e for e in r.errors)
    run_generate(c)  # 같은 날 두 번째
    titles = [t for t, _, _ in c.notifier.calls]
    assert titles.count("Claude API 키 오류") == 1
    assert c.notifier.calls[0][2] == "error"
    assert c.repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]["code"] == "invalid"
    kw.check_keys(c)  # 같은 날 health: 24시간 안이라 다시 확인·알림하지 않는다
    assert c.check_key.calls == []
    assert [t for t, _, _ in c.notifier.calls].count("Claude 키가 틀렸거나 만료됨") == 0
    c.clock.advance(days=1)
    run_generate(c)
    assert [t for t, _, _ in c.notifier.calls].count("Claude API 키 오류") == 2


def test_auth_error_detection() -> None:
    from nsm.engine.generate import network_error_text

    assert "키가 맞지 않거나" in network_error_text(_auth_error(403))
    assert kw.looks_like_key_error(_auth_error(403))
    assert not kw.looks_like_key_error(RuntimeError("api down"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gemini 호출 실패(HTTP 400: { error: API_KEY_INVALID })", True),
        ("Gemini 호출 실패(HTTP 403: PERMISSION_DENIED)", True),
        ("Gemini 호출 실패(HTTP 401: UNAUTHENTICATED)", True),
        ("Gemini 호출 실패(HTTP 429: RESOURCE_EXHAUSTED)", False),
        ("Gemini 호출 실패(HTTP 4010)", False),
    ],
)
def test_gemini_key_error_text(text, expected) -> None:
    assert kw.looks_like_key_error(RuntimeError(text)) is expected


def test_classifier_switch_on_gemini_key_error_alerts_key(ctx) -> None:
    from nsm.runtime import _on_classifier_switch

    e = RuntimeError("Gemini 호출 실패(HTTP 400: API_KEY_INVALID)")
    _on_classifier_switch(ctx, "gemini", "claude", e)
    _on_classifier_switch(ctx, "gemini", "claude", e)
    titles = _titles(ctx)
    assert titles.count("분류기 대체") == 2 and titles.count("Gemini API 키 오류") == 1
    assert ctx.repo.get_setting(kw.CHECK_KEY)["gemini_api_key"]["code"] == "invalid"
    _on_classifier_switch(ctx, "gemini", "claude", RuntimeError("HTTP 503"))
    assert _titles(ctx).count("Gemini API 키 오류") == 1


# ------------------------------------------------------------------ 화면 값


def test_banner_levels(ctx) -> None:
    now, tz = ctx.clock.now(), ctx.tz
    assert kw.key_banner(ctx.repo, now, tz) is None
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=8))
    assert kw.key_banner(ctx.repo, now, tz) is None
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) + timedelta(days=3))
    assert kw.key_banner(ctx.repo, now, tz) == {
        "lines": ["Claude 키가 3일 뒤 만료됩니다"],
        "level": "warn",
    }
    kw.record_check(ctx.repo, "gemini_api_key", "invalid", now)
    b = kw.key_banner(ctx.repo, now, tz)
    assert b["level"] == "bad" and "Gemini 키가 틀렸거나 만료됐습니다" in b["lines"]
    kw.set_expiry(ctx.repo, "anthropic_api_key", _today(ctx) - timedelta(days=1))
    assert "Claude 키가 만료됐습니다" in kw.key_banner(ctx.repo, now, tz)["lines"][0]


def test_parse_date() -> None:
    assert kw.parse_date("2026-12-12") == date(2026, 12, 12)
    for bad in ("2026-13-01", "12/12/2026", "2026-1-1", "", "내일"):
        assert kw.parse_date(bad) is None


def test_build_context_wires_secrets_and_check_key(db, app_config) -> None:
    from contextlib import ExitStack

    from nsm.app.keys import check_key
    from nsm.runtime import build_context

    secrets = MemorySecretStore()
    with ExitStack() as stack:
        c = build_context(app_config, db, stack, secrets=secrets)
        assert c.secrets is secrets and c.check_key is check_key
