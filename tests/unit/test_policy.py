from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from nsm.domain.labels import DEFAULT_BANNED_PATTERNS
from nsm.domain.policy import (
    assign_platforms,
    choose_template,
    daily_content_target,
    decide_publish,
    find_banned,
    local_day_bounds,
    under_cost_cap,
)


def _decide(**kw):
    base = dict(
        claim_source_ids=["s1"],
        existing_source_ids={"s1"},
        relevance=0.9,
        threshold=0.8,
        banned_hits=[],
        auto_publish_enabled=True,
        human_approved=20,
        protect_count=20,
    )
    return decide_publish(**{**base, **kw})


def test_all_clear_is_approved() -> None:
    d = _decide()
    assert d.status == "approved" and d.reasons == [] and d.reason_text is None


def test_each_rule_sends_to_review() -> None:
    assert "자동 게시가 꺼져" in _decide(auto_publish_enabled=False).reason_text
    assert "보호 모드: 사람 승인 19/20" in _decide(human_approved=19).reason_text
    assert "출처가 확인되지 않는 주장 1건" in _decide(claim_source_ids=["s1", "ghost"]).reason_text
    assert "출처가 연결된 사실 주장이 없음" in _decide(claim_source_ids=[]).reason_text
    assert "0.79 < 0.80" in _decide(relevance=0.79).reason_text
    assert "금칙어" in _decide(banned_hits=["최고"]).reason_text
    for kw in [
        {"auto_publish_enabled": False},
        {"relevance": 0.5},
        {"banned_hits": ["x"]},
    ]:
        assert _decide(**kw).status == "pending_review"


def test_find_banned_words_and_patterns(app_config) -> None:
    persona = app_config.persona.model_copy(
        update={"banned_patterns": list(DEFAULT_BANNED_PATTERNS)}
    )
    assert find_banned(["이건 최고의 원두"], persona) == ["최고"]
    hits = find_banned(["제가 직접 써봤는데 좋아요"], persona)
    assert hits and "써봤" in hits[0]
    assert find_banned(["원두 고르는 법"], persona) == []


def test_platform_assignment_follows_quotas() -> None:
    quotas = {"instagram": 1, "threads": 2, "naver_blog": 1}
    enabled = ["instagram", "threads", "naver_blog"]
    assert daily_content_target(enabled, quotas) == 2
    assert assign_platforms(0, enabled, quotas) == enabled
    assert assign_platforms(1, enabled, quotas) == ["threads"]
    assert assign_platforms(2, enabled, quotas) == []
    assert daily_content_target([], quotas) == 0


def test_choose_template_rotates() -> None:
    assert choose_template([]) == "info"
    assert choose_template(["info"]) == "list"
    assert choose_template(["list", "info"]) == "compare"
    assert choose_template(["compare", "list", "info"]) == "info"


def test_cost_cap_and_day_bounds() -> None:
    assert under_cost_cap(2.99, 3.0) and not under_cost_cap(3.0, 3.0)
    start, end = local_day_bounds(datetime(2026, 10, 10, 16, 0, tzinfo=UTC), "Asia/Seoul")
    assert start.isoformat().startswith("2026-10-11T00:00:00+09:00")
    assert (end - start).days == 1


def test_invalid_banned_pattern_rejected_at_config(app_config) -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        app_config.persona.model_validate(
            {**app_config.persona.model_dump(), "banned_patterns": ["(unclosed"]}
        )


def test_should_post_uses_posting_hours_only_when_set(app_config) -> None:
    from nsm.config.store import parse_windows
    from nsm.domain.policy import should_post

    p = app_config.persona.model_copy(update={"active_hours": parse_windows("00:00-23:59")})
    tz = "Asia/Seoul"
    night = datetime(2026, 10, 11, 3, 0, tzinfo=ZoneInfo(tz))
    assert should_post(night, p, tz, None) == (True, "")
    assert should_post(night, p, tz, parse_windows("08:00-23:00")) == (False, "게시 시간 밖")
    late = parse_windows("22:00-02:00")  # 자정을 넘는 게시 시간
    assert should_post(night.replace(hour=1), p, tz, late) == (True, "")
    off = p.model_copy(update={"enabled": False})
    assert should_post(night, off, tz, None) == (False, "페르소나가 꺼져 있음")
