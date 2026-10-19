"""대시보드 "다음 게시 시각"(C26)과 그 바탕인 정책 도우미 next_window_start."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from nsm.app.next_publish import next_publishes
from nsm.domain.models import ActiveWindow
from nsm.domain.policy import next_window_start
from nsm.engine.publish import PAUSED_KEY

KST = ZoneInfo("Asia/Seoul")
WIN = [ActiveWindow(start=time(8, 0), end=time(23, 0))]


def _at(h: int, m: int = 0, day: int = 19) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=KST)


def test_next_window_start_now_inside_or_next_opening() -> None:
    assert next_window_start(_at(10), WIN, "Asia/Seoul") == _at(10)
    assert next_window_start(_at(23, 30), WIN, "Asia/Seoul") == _at(8, day=20)
    assert next_window_start(_at(3), WIN, "Asia/Seoul") == _at(8)
    assert next_window_start(_at(3), [], "Asia/Seoul") is None
    # 자정을 넘는 구간(22:00-02:00): 03시면 그날 22시
    night = [ActiveWindow(start=time(22, 0), end=time(2, 0))]
    assert next_window_start(_at(3), night, "Asia/Seoul") == _at(22)


class _Repo:
    def __init__(self, queued=(), failed=(), last=None, published_today=0, paused=()):
        self._q, self._f, self._last, self._n, self._paused = (
            list(queued),
            list(failed),
            last,
            published_today,
            list(paused),
        )

    def get_setting(self, key, default=None):
        return self._paused if key == PAUSED_KEY else default

    def list_contents(self, status=None, limit=200):
        # 승인된 원고 c-ok 하나만. 테스트의 게시 행은 기본으로 이 원고 것이다
        return [SimpleNamespace(id="c-ok")] if status == "approved" else []

    def publications_in_status(self, status):
        return self._q if status == "queued" else self._f

    def last_published_at(self, platform):
        return self._last

    def count_published_between(self, platform, start, end):
        return self._n


def _pub(platform, status="queued", next_attempt_at=None, content_id="c-ok"):
    return SimpleNamespace(
        platform=platform, status=status, next_attempt_at=next_attempt_at, content_id=content_id
    )


def _cfg(**over):
    settings = SimpleNamespace(
        timezone="Asia/Seoul",
        enabled_platforms=["instagram", "threads"],
        min_hours_between_posts=3.0,
        quotas={"instagram": 1, "threads": 2},
        posting_hours=WIN,
    )
    for k, v in over.items():
        setattr(settings, k, v)
    return SimpleNamespace(
        settings=settings, persona=SimpleNamespace(enabled=True, active_hours=WIN)
    )


def test_nothing_waiting_gives_no_rows() -> None:
    assert next_publishes(_Repo(), _cfg(), _at(10)) == []


def test_waiting_now_inside_window_is_now() -> None:
    rows = next_publishes(_Repo(queued=[_pub("threads")]), _cfg(), _at(10))
    assert [(r.platform, r.at) for r in rows] == [("threads", _at(10))]


def test_gap_retry_quota_and_window_push_the_time() -> None:
    now = _at(10)
    # 같은 플랫폼 3시간 간격: 08:30에 올렸으면 11:30
    rows = next_publishes(_Repo(queued=[_pub("threads")], last=_at(8, 30)), _cfg(), now)
    assert rows[0].at == _at(11, 30)
    # 실패 재시도 시각이 더 늦으면 그 시각
    rows = next_publishes(
        _Repo(failed=[_pub("instagram", "failed", _at(12, 5).astimezone(UTC))]), _cfg(), now
    )
    assert rows[0].at == _at(12, 5)
    # 오늘 한도를 다 썼으면 내일 게시 시간 시작(08:00)
    rows = next_publishes(_Repo(queued=[_pub("instagram")], published_today=1), _cfg(), now)
    assert rows[0].at == _at(8, day=20)
    # 게시 시간 밖(23:30)이면 내일 08:00
    rows = next_publishes(_Repo(queued=[_pub("threads")]), _cfg(), _at(23, 30))
    assert rows[0].at == _at(8, day=20)


def test_only_approved_content_counts_and_missing_adapter_is_reported() -> None:
    now = _at(10)
    # 검토 대기·폐기 원고의 queued 행은 세지 않는다
    assert (
        next_publishes(_Repo(queued=[_pub("threads", content_id="c-pending")]), _cfg(), now) == []
    )
    # 실패 재시도만 승인된 원고면 그 시각이 보인다(다른 원고의 queued 행에 가려지지 않음)
    rows = next_publishes(
        _Repo(
            queued=[_pub("instagram", content_id="c-pending")],
            failed=[_pub("instagram", "failed", _at(12, 5).astimezone(UTC))],
        ),
        _cfg(),
        now,
    )
    assert rows[0].at == _at(12, 5)
    # 어댑터가 없는 플랫폼은 "연결 안 됨"
    rows = next_publishes(_Repo(queued=[_pub("instagram")]), _cfg(), now, {"threads": object()})
    assert rows[0].at is None and rows[0].reason == "연결 안 됨"


def test_posting_hours_and_active_hours_both_apply() -> None:
    # 활동 09~18, 게시 08~20: 08시는 게시 시간이지만 활동 시간 밖 → 09:00
    cfg = _cfg(posting_hours=[ActiveWindow(start=time(8, 0), end=time(20, 0))])
    cfg.persona.active_hours = [ActiveWindow(start=time(9, 0), end=time(18, 0))]
    rows = next_publishes(_Repo(queued=[_pub("threads")]), cfg, _at(7))
    assert rows[0].at == _at(9)


def test_paused_disabled_and_rate_limited() -> None:
    now = _at(10)
    rows = next_publishes(_Repo(queued=[_pub("threads")], paused=["threads"]), _cfg(), now)
    assert rows[0].at is None and "중지" in rows[0].reason
    cfg = _cfg()
    cfg.persona.enabled = False
    rows = next_publishes(_Repo(queued=[_pub("threads")]), cfg, now)
    assert rows[0].at is None and "페르소나" in rows[0].reason
    blocked = SimpleNamespace(blocked_until=now + timedelta(hours=2))
    rows = next_publishes(_Repo(queued=[_pub("threads")]), _cfg(), now, {"threads": blocked})
    assert rows[0].at == _at(12)
