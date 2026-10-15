"""설정 화면(M17): 페르소나·브랜드·운영 값을 대시보드에서 고친다."""

from __future__ import annotations

from datetime import time

import pytest

from nsm.app.settings_form import SettingsError, apply_settings_form, form_values
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처를 쓴다


def _form(cfg, **over) -> dict[str, str]:
    v = form_values(cfg)
    v.update(over)
    return v


def test_settings_page_shows_current_values(env) -> None:  # noqa: F811
    cfg = env["holder"]["cfg"]
    html = env["client"].get("/settings").text
    assert cfg.persona.name in html and cfg.brand.name in html
    assert 'name="topics"' in html and 'name="posting_hours"' in html
    assert 'href="/settings"' in html  # 메뉴
    assert "nsm init" in html  # 계정·드라이런·키는 여기서 바꾸지 않는다고 안내


def test_save_persona_brand_and_operation(env) -> None:  # noqa: F811
    cfg = env["holder"]["cfg"]
    r = _post(
        env,
        "/settings",
        **_form(
            cfg,
            persona_name="하루",
            topics="핸드드립\n원두\n\n원두",
            brand_name="하루의 커피노트",
            industry="커피 정보",
            products="",
            primary_color="#2A6F4E",
            active_hours="00:00-23:59",
            posting_hours="08:00-23:00",
            quota_instagram="2",
            daily_cost_cap_usd="1.5",
            ad_label="on",
        ),
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    new = env["holder"]["cfg"]
    assert new.persona.name == "하루" and new.persona.topics == ["핸드드립", "원두"]
    assert new.persona.handle == cfg.persona.handle  # 계정은 그대로
    assert new.persona.ad_label is True and new.persona.enabled == cfg.persona.enabled
    assert (new.brand.name, new.brand.industry, new.brand.products) == (
        "하루의 커피노트",
        "커피 정보",
        [],
    )
    assert new.brand.primary_color == "#2A6F4E"
    assert new.settings.posting_hours[0].start == time(8, 0)
    assert new.settings.quotas["instagram"] == 2 and new.settings.daily_cost_cap_usd == 1.5
    assert env["saved"], "설정 파일 저장이 호출돼야 한다"
    assert "저장했습니다" in env["client"].get("/settings?saved=1").text


def test_protected_values_cannot_be_changed_from_the_form(env) -> None:  # noqa: F811
    cfg = env["holder"]["cfg"]
    _post(
        env,
        "/settings",
        **_form(cfg, dry_run="false", accounts="@evil", handle="@evil", protect_count="0"),
    )
    new = env["holder"]["cfg"]
    assert new.settings.dry_run == cfg.settings.dry_run
    assert new.settings.accounts == cfg.settings.accounts
    assert new.persona.handle == cfg.persona.handle
    assert new.settings.protect_count == cfg.settings.protect_count


def test_empty_posting_hours_means_same_as_active(env) -> None:  # noqa: F811
    _post(env, "/settings", **_form(env["holder"]["cfg"], posting_hours=""))
    assert env["holder"]["cfg"].settings.posting_hours is None


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        ({"topics": "\n"}, "관심 주제"),
        ({"persona_name": " "}, "페르소나 이름"),
        ({"brand_name": ""}, "브랜드 이름"),
        ({"primary_color": "blue"}, "주 색"),
        ({"banned_patterns": "(깨진"}, "금칙 패턴"),
        ({"active_hours": "아침"}, "활동 시간"),
        ({"active_hours": ""}, "활동 시간"),
        ({"posting_hours": "25:00-26:00"}, "게시 시간"),
        ({"quota_threads": "11"}, "스레드"),
        ({"daily_cost_cap_usd": "abc"}, "비용"),
        ({"topics": "\n".join(f"주제{i}" for i in range(11))}, "10개"),
    ],
)
def test_invalid_values_show_error_and_save_nothing(env, over, msg) -> None:  # noqa: F811
    before = env["holder"]["cfg"]
    r = _post(env, "/settings", **_form(before, **over))
    assert r.status_code == 400 and msg in r.text and "저장하지 못했습니다" in r.text
    assert env["holder"]["cfg"] == before and not env["saved"]


def test_error_page_keeps_what_was_typed(env) -> None:  # noqa: F811
    r = _post(
        env, "/settings", **_form(env["holder"]["cfg"], persona_name="새이름", primary_color="x")
    )
    assert 'value="새이름"' in r.text


def test_csrf_required(env) -> None:  # noqa: F811
    r = env["client"].post("/settings", data=form_values(env["holder"]["cfg"]))
    assert r.status_code == 403


def test_apply_settings_form_is_pure(app_config) -> None:
    new = apply_settings_form(app_config, _form(app_config, tone="짧게"))
    assert new.persona.tone == "짧게" and app_config.persona.tone != "짧게"
    with pytest.raises(SettingsError):
        apply_settings_form(app_config, _form(app_config, quota_instagram="-1"))


def test_reply_interval_is_editable_with_a_floor(env) -> None:  # noqa: F811
    cfg = env["holder"]["cfg"]
    assert 'name="reply_interval_minutes"' in env["client"].get("/settings").text
    _post(env, "/settings", **_form(cfg, reply_interval_minutes="7"))
    assert env["holder"]["cfg"].settings.reply_interval_minutes == 7
    r = _post(env, "/settings", **_form(cfg, reply_interval_minutes="1"))
    assert r.status_code == 400 and "댓글 확인 간격" in r.text


def test_instagram_comment_interval_is_editable(env) -> None:  # noqa: F811
    """인스타그램 댓글 확인 간격은 따로(10~180분, ADR 0038). 칸이 없는 옛 폼은 지금 값을 지킨다."""
    cfg = env["holder"]["cfg"]
    assert 'name="instagram_comment_interval_minutes"' in env["client"].get("/settings").text
    _post(env, "/settings", **_form(cfg, instagram_comment_interval_minutes="45"))
    assert env["holder"]["cfg"].settings.instagram_comment_interval_minutes == 45
    r = _post(env, "/settings", **_form(cfg, instagram_comment_interval_minutes="5"))
    assert r.status_code == 400 and "인스타그램 댓글 확인 간격" in r.text
    old = _form(env["holder"]["cfg"])
    old.pop("instagram_comment_interval_minutes")
    _post(env, "/settings", **old)
    assert env["holder"]["cfg"].settings.instagram_comment_interval_minutes == 45


def test_recent_period_is_editable(env) -> None:  # noqa: F811
    cfg = env["holder"]["cfg"]
    html = env["client"].get("/settings").text
    assert 'name="recent_period"' in html and "최근 1주" in html
    _post(env, "/settings", **_form(cfg, recent_period="1d"))
    assert env["holder"]["cfg"].settings.collect.recent_period == "1d"
    r = _post(env, "/settings", **_form(cfg, recent_period="10y"))
    assert r.status_code == 400 and "자료 검색 기간" in r.text


def test_card_style_is_selectable(env) -> None:  # noqa: F811
    """S8: 카드 스타일 전역 선택(brand.card_style). 칸이 없는 옛 폼은 지금 값을 지킨다."""
    cfg = env["holder"]["cfg"]
    html = env["client"].get("/settings").text
    assert 'name="card_style"' in html and "일러스트형" in html
    _post(env, "/settings", **_form(cfg, card_style="illust"))
    assert env["holder"]["cfg"].brand.card_style == "illust"
    old_form = _form(env["holder"]["cfg"])
    old_form.pop("card_style")
    _post(env, "/settings", **old_form)
    assert env["holder"]["cfg"].brand.card_style == "illust"
    r = _post(env, "/settings", **_form(cfg, card_style="neon"))
    assert r.status_code == 400 and "카드 스타일" in r.text
