"""C53: 설정 → API 키의 만료일 메모, 저장·확인 결과 반영, 첫 화면 배너(간단 모드 포함).

가짜 키·가짜 check_key만 쓴다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from nsm.app.server import UI_MODE_KEY
from nsm.engine import key_watch as kw
from tests.app.test_keys_routes import GEMINI, KEY, KEY2, _page, _post
from tests.app.test_keys_routes import env as env  # noqa: F401  픽스처를 그대로 쓴다

URL = "/settings/keys/gemini_api_key"


def _today(env):
    s = env["state"]
    return kw.today(s.clock.now(), env["holder"]["cfg"].settings.timezone)


def _memo(env) -> dict:
    return env["state"].repo.get_setting(kw.EXPIRY_KEY, {})


def _row(html: str, label: str, nxt: str | None) -> str:
    a = html.index(f"<b>{label}</b>")
    return html[a : html.index(f"<b>{nxt}</b>")] if nxt else html[a:]


def test_form_has_optional_date_field(env) -> None:
    row = _row(_page(env), "Claude", "Gemini")
    assert 'type="date" name="expires"' in row and "만료일(없으면 비움)" in row  # C54 라벨


def test_date_only_save_update_and_clear(env) -> None:
    d = _today(env) + timedelta(days=21)
    r = _post(env, URL, value="", with_date="1", expires=d.isoformat())
    assert _memo(env) == {"gemini_api_key": d.isoformat()}
    assert env["secrets"].get("gemini_api_key") == GEMINI and env["changed"] == []
    assert env["checked"] == []  # 날짜만 바꿀 때는 키 확인을 하지 않는다
    html = _page(env, r)
    row = _row(html, "Gemini", "OpenAI")
    assert f"만료 {d:%m-%d} (21일 남음)" in row and f'value="{d.isoformat()}"' in row
    assert "badge warn key-expiry" not in row  # 7일 넘게 남으면 배지 없이
    d2 = _today(env) + timedelta(days=5)
    r = _post(env, URL, value="", with_date="1", expires=d2.isoformat())
    assert _memo(env) == {"gemini_api_key": d2.isoformat()}
    assert "badge warn key-expiry" in _row(_page(env, r), "Gemini", "OpenAI")
    r = _post(env, URL, value="", with_date="1", expires="")  # 둘 다 비우고 저장: 메모 삭제
    assert _memo(env) == {} and "만료일 메모를 지웠습니다" in _page(env, r)


def test_past_date_is_saved_and_shown_expired(env) -> None:
    d = _today(env) - timedelta(days=2)
    r = _post(env, URL, value="", with_date="1", expires=d.isoformat())
    assert _memo(env) == {"gemini_api_key": d.isoformat()}
    assert f'badge bad key-expiry">만료됨({d:%m-%d})' in _row(_page(env, r), "Gemini", "OpenAI")


@pytest.mark.parametrize("bad", ["2026-13-40", "12/12/2026", "내일"])
def test_bad_date_saves_nothing(env, bad) -> None:
    r = _post(env, "/settings/keys/anthropic_api_key", value=KEY, with_date="1", expires=bad)
    assert "만료일 형식이 아닙니다" in _page(env, r)
    assert env["secrets"].get("anthropic_api_key") is None and _memo(env) == {}


def test_date_only_without_key_is_refused(env) -> None:
    r = _post(
        env, "/settings/keys/anthropic_api_key", value="", with_date="1", expires="2026-12-12"
    )
    assert "키가 없어 만료일만 적을 수 없습니다" in _page(env, r) and _memo(env) == {}


def test_blank_key_without_date_field_is_still_format_error(env) -> None:
    r = _post(env, "/settings/keys/anthropic_api_key", value="")  # 예전 폼(날짜 칸 없음)
    assert "키 형식이 아닙니다" in _page(env, r)


def test_new_key_replaces_old_date_and_state(env) -> None:
    repo, now = env["state"].repo, env["state"].clock.now()
    old = _today(env) + timedelta(days=2)
    _post(
        env, "/settings/keys/anthropic_api_key", value=KEY, with_date="1", expires=old.isoformat()
    )
    assert _memo(env) == {"anthropic_api_key": old.isoformat()}
    repo.set_setting(kw.NOTIFIED_KEY, {"anthropic_api_key": {"date": old.isoformat(), "stage": 3}})
    kw.record_check(repo, "anthropic_api_key", "invalid", now)
    _post(
        env, "/settings/keys/anthropic_api_key", value=KEY2, with_date="1", expires=""
    )  # 새 키, 날짜 없음
    assert _memo(env) == {}  # 옛 키의 날짜가 남지 않는다
    assert repo.get_setting(kw.NOTIFIED_KEY) == {}
    st = repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]
    assert st["code"] == "ok"  # 저장 때 확인 결과가 바로 반영(옛 invalid 배너 사라짐)
    new = _today(env) + timedelta(days=30)
    _post(
        env, "/settings/keys/anthropic_api_key", value=KEY, with_date="1", expires=new.isoformat()
    )
    assert _memo(env) == {"anthropic_api_key": new.isoformat()}


def test_unverifiable_new_key_clears_old_check(env) -> None:
    repo = env["state"].repo
    kw.record_check(repo, "openai_api_key", "invalid", env["state"].clock.now())
    env["state"].check_key = lambda n, v: ("network", None)
    _post(env, "/settings/keys/openai_api_key", value=KEY)
    assert "openai_api_key" not in repo.get_setting(kw.CHECK_KEY)  # 다음 점검에서 바로 확인


def test_check_button_records_result(env) -> None:
    repo = env["state"].repo
    env["state"].check_key = lambda n, v: ("invalid", 400)
    r = _post(env, f"{URL}/check")
    assert repo.get_setting(kw.CHECK_KEY)["gemini_api_key"]["code"] == "invalid"
    row = _row(_page(env, r), "Gemini", "OpenAI")
    assert "자동 확인" in row and "키가 틀렸거나 만료됨" in row
    env["state"].check_key = lambda n, v: ("ok", 200)
    _post(env, f"{URL}/check")
    st = repo.get_setting(kw.CHECK_KEY)["gemini_api_key"]
    assert st["code"] == "ok"
    assert kw.key_banner(repo, env["state"].clock.now(), "Asia/Seoul") is None


def test_delete_key_clears_memo_and_state(env) -> None:
    repo = env["state"].repo
    d = _today(env) + timedelta(days=3)
    _post(env, URL, value="", with_date="1", expires=d.isoformat())
    repo.set_setting(kw.NOTIFIED_KEY, {"gemini_api_key": {"date": d.isoformat(), "stage": 3}})
    kw.record_check(repo, "gemini_api_key", "ok", env["state"].clock.now())
    _post(env, f"{URL}/delete")
    assert _memo(env) == {} and repo.get_setting(kw.NOTIFIED_KEY) == {}
    assert repo.get_setting(kw.CHECK_KEY) == {}


def test_env_key_can_have_date_only(env) -> None:
    env["secrets"].env["openai_api_key"] = "fake-openai-env-key-DDDDDDDDDDDD"
    row = _row(_page(env), "OpenAI", None)
    assert 'name="value"' not in row and 'name="expires"' in row
    d = _today(env) + timedelta(days=10)
    _post(env, "/settings/keys/openai_api_key", value="", with_date="1", expires=d.isoformat())
    assert _memo(env) == {"openai_api_key": d.isoformat()}


def test_auto_check_time_shown(env) -> None:
    repo, now = env["state"].repo, env["state"].clock.now()
    kw.record_check(repo, "gemini_api_key", "ok", now)
    row = _row(_page(env), "Gemini", "OpenAI")
    assert f"자동 확인 {now:%m-%d %H:%M} 연결됨" in row


@pytest.mark.parametrize("mode", ["simple", "advanced"])
def test_overview_banner_in_both_modes(env, mode) -> None:
    repo = env["state"].repo
    repo.set_setting(UI_MODE_KEY, mode)
    c = env["client"]
    assert "key-banner" not in c.get("/").text
    kw.set_expiry(repo, "gemini_api_key", _today(env) + timedelta(days=3))
    html = c.get("/").text
    assert "notice warn key-banner" in html
    assert "Gemini 키가 3일 뒤 만료됩니다" in html and 'href="/settings#api-keys"' in html
    kw.record_check(repo, "anthropic_api_key", "invalid", env["state"].clock.now())
    html = c.get("/").text
    assert "notice bad key-banner" in html and "Claude 키가 틀렸거나 만료됐습니다" in html
    assert KEY not in html and GEMINI not in html


def test_overview_banner_hidden_for_far_date(env) -> None:
    kw.set_expiry(env["state"].repo, "gemini_api_key", _today(env) + timedelta(days=8))
    assert "key-banner" not in env["client"].get("/").text


def test_replacing_key_with_prefilled_old_date_drops_it(env) -> None:
    """리뷰: 날짜 칸에 옛 날짜가 미리 채워져 그대로 보내도 새 키에 옛 날짜가 남지 않는다."""
    old = _today(env) - timedelta(days=1)
    _post(
        env, "/settings/keys/anthropic_api_key", value=KEY, with_date="1", expires=old.isoformat()
    )
    _post(
        env, "/settings/keys/anthropic_api_key", value=KEY2, with_date="1", expires=old.isoformat()
    )
    assert _memo(env) == {}
    assert "key-banner" not in env["client"].get("/").text
