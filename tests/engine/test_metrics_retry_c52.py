"""C52: 지표 읽기에서 글 페이지가 안 열린 일시적 실패(code='page')는 쉬고 한 번만 다시 읽는다.

- 다시 읽어 성공하면 오류가 남지 않고 실행 기록에 "페이지 오류로 다시 읽음 n개"가 보인다.
- 또 실패하면 지금처럼 오류 1건. 글 하나당 다시 읽기는 1회.
- 로그인·요청 제한(재시도 시각)·화면 변경·끄는 중 끊김은 다시 읽지 않는다.
- 쉬는 동안 끄기 시작하면 다시 읽지 않고 중단(오류 기록 없음).
진짜로 기다리지 않게 sleep을 바꾼다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.adapters.platforms.aside_common import PlatformError, RateLimitedError
from nsm.engine import health_metrics
from nsm.engine.context import JobContext
from nsm.engine.health_metrics import RETRY_WAIT_S, run_metrics
from nsm.engine.summary import describe
from nsm.store.repo import Repository
from tests.engine.test_health_metrics import _published, _read_ids
from tests.fakes import FakeNotifier, FakePlatformAdapter

HTTP500 = "instagram: 페이지를 열 수 없음(HTTP ERROR 500)"


@pytest.fixture
def ctx(db, clock, app_config) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram")},
    )


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    got: list[float] = []
    monkeypatch.setattr(health_metrics, "sleep", got.append)
    return got


def _flaky(adapter, name: str, errors: list[BaseException]):
    """앞에서부터 errors를 하나씩 던지고, 다 쓰면 원래 메서드를 부른다. 부른 횟수를 돌려준다."""
    orig = getattr(adapter, name)
    calls: list[int] = []

    def read(*a):
        calls.append(1)
        if errors:
            raise errors.pop(0)
        return orig(*a)

    setattr(adapter, name, read)
    return calls


def _page() -> PlatformError:
    return PlatformError(HTTP500, "transient", "page")


def test_page_failure_once_then_success_is_saved(ctx, app_config, waits) -> None:
    ext = _published(ctx, app_config, 0, 1)
    calls = _flaky(ctx.adapters["instagram"], "read_post_metrics", [_page()])
    r = run_metrics(ctx)
    assert len(calls) == 2 and waits == [RETRY_WAIT_S]
    assert ext in _read_ids(ctx)
    assert r.errors == [] and r.ok
    assert r.counts["page_retried"] == 1
    assert "페이지 오류로 다시 읽음 1개" in describe("metrics", counts=r.counts)


def test_page_failure_twice_is_one_error_and_no_third_try(ctx, app_config, waits) -> None:
    _published(ctx, app_config, 0, 1)
    calls = _flaky(ctx.adapters["instagram"], "read_post_metrics", [_page(), _page(), _page()])
    r = run_metrics(ctx)
    assert len(calls) == 2 and len(waits) == 1
    assert r.errors == [f"instagram 게시물 지표 실패: {HTTP500}"]
    assert "page_retried" not in r.counts
    assert "다시 읽음" not in describe("metrics", counts=r.counts)


def test_second_failure_of_other_kind_follows_its_own_branch(ctx, app_config, waits) -> None:
    _published(ctx, app_config, 0, 1)
    until = ctx.clock.now() + timedelta(hours=1)
    calls = _flaky(
        ctx.adapters["instagram"],
        "read_post_metrics",
        [_page(), RateLimitedError("instagram: 요청이 많음(429)", until)],
    )
    r = run_metrics(ctx)
    assert len(calls) == 2
    assert r.counts["rate_limited"] and not any("instagram" in e for e in r.errors)


def test_auth_failure_is_not_retried(ctx, app_config, waits) -> None:
    _published(ctx, app_config, 0, 1)
    calls = _flaky(
        ctx.adapters["instagram"],
        "read_post_metrics",
        [PlatformError("instagram: 로그인 필요", "auth"), _page()],
    )
    r = run_metrics(ctx)
    assert len(calls) == 1 and waits == []
    assert len(r.errors) == 1 and "로그인 필요" in r.errors[0]


@pytest.mark.parametrize(
    ("kind", "code"),
    [("ui_changed", "page"), ("transient", None), ("transient", "interrupted")],
)
def test_non_page_failures_are_not_retried(ctx, app_config, waits, kind, code) -> None:
    _published(ctx, app_config, 0, 1)
    calls = _flaky(
        ctx.adapters["instagram"],
        "read_post_metrics",
        [PlatformError("instagram: Selector not found", kind, code), _page()],
    )
    run_metrics(ctx)
    assert len(calls) == 1 and waits == []


def test_page_failure_with_retry_at_raises_like_before(ctx, app_config, waits) -> None:
    _published(ctx, app_config, 0, 1)
    e = _page()
    e.retry_at = ctx.clock.now() + timedelta(hours=1)  # type: ignore[attr-defined]
    calls = _flaky(ctx.adapters["instagram"], "read_post_metrics", [e])
    r = run_metrics(ctx)
    assert len(calls) == 1 and waits == []
    assert r.counts["rate_limited"] and not any("instagram" in x for x in r.errors)


def test_shutdown_during_wait_stops_without_retry_or_error(ctx, app_config, monkeypatch) -> None:
    _published(ctx, app_config, 0, 1)
    calls = _flaky(ctx.adapters["instagram"], "read_post_metrics", [_page()])
    stopping = {"on": False}
    ctx.shutting_down = lambda: stopping["on"]
    monkeypatch.setattr(health_metrics, "sleep", lambda s: stopping.update(on=True))
    r = run_metrics(ctx)
    assert len(calls) == 1
    assert r.interrupted and not r.ok
    assert r.errors == [] and not ctx.notifier.calls
    assert "page_retried" not in r.counts


def test_account_metrics_page_failure_is_retried_once(ctx, waits) -> None:
    calls = _flaky(ctx.adapters["threads"], "read_account_metrics", [_page()])
    r = run_metrics(ctx)
    assert len(calls) == 2 and r.errors == []
    assert r.counts["accounts"] == 2 and r.counts["page_retried"] == 1

    calls = _flaky(ctx.adapters["threads"], "read_account_metrics", [_page(), _page()])
    r = run_metrics(ctx)
    assert len(calls) == 2
    assert r.errors == [f"threads 계정 지표 실패: {HTTP500}"]


def test_no_retry_keeps_summary_unchanged(ctx, app_config, waits) -> None:
    _published(ctx, app_config, 0, 1, "threads")
    r = run_metrics(ctx)
    assert waits == [] and "page_retried" not in r.counts
    assert describe("metrics", counts=r.counts).startswith("게시물 지표 1개, 계정 지표 2개 읽음")
    assert "다시 읽음" not in describe("metrics", counts=r.counts)
