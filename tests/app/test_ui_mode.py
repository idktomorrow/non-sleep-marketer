"""화면 모드(C13): 간단(기본)은 매일 쓰는 화면만, 고급은 지금까지의 화면 전부. 기능은 같다."""

from __future__ import annotations

import pytest

from nsm.app.server import UI_MODE_KEY
from nsm.app.settings_form import form_values
from tests.app.test_dashboard import env  # noqa: F401  같은 대시보드 픽스처


@pytest.fixture
def uenv(env, tmp_path):  # noqa: F811
    env["state"].uploads_dir = tmp_path / "uploads"
    return env


def _mode(e, mode: str, referer: str = "http://testserver/", csrf: str | None = None):
    return e["client"].post(
        "/ui-mode",
        data={"csrf": e["state"].csrf_token if csrf is None else csrf, "mode": mode},
        headers={"referer": referer},
        follow_redirects=False,
    )


def test_simple_mode_is_default_and_shows_only_daily_screens(uenv) -> None:
    html = uenv["client"].get("/").text
    assert 'value="simple" class="on"' in html  # 스위치가 간단에 켜져 있다
    assert 'href="/materials"' not in html and 'href="/logs"' not in html  # 메뉴에서 빠짐
    for kept in ("/review", "/queue", "/comments", "/performance", "/reports", "/settings"):
        assert f'href="{kept}"' in html
    # 지금 실행은 몇 개만, 최근 실행 표는 없음
    assert 'action="/jobs/collect/run"' in html and 'action="/jobs/reply/run"' in html
    assert 'action="/jobs/maintenance/run"' not in html and 'action="/jobs/health/run"' not in html
    assert "최근 실행" not in html
    # 자료 올리기는 "오늘"에서 바로
    assert 'action="/materials/upload"' in html and 'name="next" value="/"' in html
    # 스위치(페르소나·자동 게시)와 할 일은 그대로
    assert 'action="/persona/toggle"' in html and "검토할 원고" in html


def test_switch_to_advanced_shows_everything(uenv) -> None:
    r = _mode(uenv, "advanced", "http://testserver/review")
    assert r.status_code == 303 and r.headers["location"] == "/review"  # 있던 화면에 머문다
    assert uenv["repo"].get_setting(UI_MODE_KEY) == "advanced"
    html = uenv["client"].get("/").text
    assert 'value="advanced" class="on"' in html
    assert 'href="/materials"' in html and 'href="/logs"' in html
    assert 'action="/jobs/maintenance/run"' in html and "최근 실행" in html
    assert 'name="next" value="/"' not in html  # 고급은 자료 화면에서 올린다


def test_switch_to_simple_from_hidden_screen_goes_home(uenv) -> None:
    _mode(uenv, "advanced")
    assert _mode(uenv, "simple", "http://testserver/logs?all=1").headers["location"] == "/"
    assert _mode(uenv, "simple", "http://testserver/materials").headers["location"] == "/"
    assert _mode(uenv, "simple", "http://testserver/settings").headers["location"] == "/settings"
    # 다른 곳에서 온 referer는 따르지 않는다
    assert _mode(uenv, "simple", "http://evil.example/review").headers["location"] == "/"


def test_bad_mode_and_csrf_are_rejected(uenv) -> None:
    assert _mode(uenv, "expert").status_code == 400
    assert _mode(uenv, "advanced", csrf="wrong").status_code == 403
    assert uenv["repo"].get_setting(UI_MODE_KEY) is None


def test_unknown_stored_mode_falls_back_to_simple(uenv) -> None:
    uenv["repo"].set_setting(UI_MODE_KEY, "weird")
    assert 'value="simple" class="on"' in uenv["client"].get("/").text


def test_hidden_screens_still_open_in_simple_mode(uenv) -> None:
    for path in ("/materials", "/logs"):
        r = uenv["client"].get(path)
        assert r.status_code == 200
        assert f'href="{path}" class="active"' in r.text  # 지금 있는 화면 탭은 보인다


def test_simple_settings_hide_detail_fields_but_still_save_them(uenv) -> None:
    html = uenv["client"].get("/settings").text
    assert html.count('class="field-row adv-only" hidden') == 2
    for name in ("active_hours", "reply_interval_minutes", "min_hours_between_posts"):
        assert f'name="{name}"' in html  # 숨겨도 폼에 있어 저장할 때 같이 간다
    for name in ("posting_hours", "quota_instagram", "daily_cost_cap_usd", "card_style"):
        assert f'name="{name}"' in html
    # 간단 화면에서 보이는 값만 바꿔 저장해도 숨은 값은 그대로
    cfg = uenv["state"].get_config()
    form = {**form_values(cfg), "quota_threads": "2", "csrf": uenv["state"].csrf_token}
    assert uenv["client"].post("/settings", data=form, follow_redirects=False).status_code == 303
    new = uenv["state"].get_config().settings
    assert new.quotas["threads"] == 2
    assert new.reply_interval_minutes == cfg.settings.reply_interval_minutes
    assert uenv["state"].get_config().persona.active_hours == cfg.persona.active_hours
    _mode(uenv, "advanced")
    assert 'adv-only" hidden' not in uenv["client"].get("/settings").text


def _upload(e, nxt: str | None):
    data = {"csrf": e["state"].csrf_token}
    if nxt is not None:
        data["next"] = nxt
    return e["client"].post(
        "/materials/upload",
        data=data,
        files={"file": ("안내.txt", "치이카와 팝업 10월 20일 성수동 오픈".encode())},
        follow_redirects=False,
    )


def test_upload_from_home_returns_home_with_notice(uenv) -> None:
    r = _upload(uenv, "/")
    assert r.status_code == 303 and r.headers["location"].startswith("/?uploaded=")
    html = uenv["client"].get(r.headers["location"]).text
    assert "올렸습니다" in html and "글자 22자를 읽었습니다" in html
    bad = uenv["client"].post(
        "/materials/upload",
        data={"csrf": uenv["state"].csrf_token, "next": "/"},
        files={"file": ("사진.png", b"\x89PNG")},
        follow_redirects=False,
    )
    assert bad.headers["location"].startswith("/?error=")
    assert '<div class="notice bad">' in uenv["client"].get(bad.headers["location"]).text


def test_upload_next_only_accepts_home(uenv) -> None:
    for nxt in (None, "https://evil.example/", "/logs"):
        assert _upload(uenv, nxt).headers["location"].startswith("/materials?uploaded=")
