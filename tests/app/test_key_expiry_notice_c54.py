"""C54: 새 키 저장 결과 문구가 만료일 처리(적음·안 적음·옛 날짜 지움)를 보여 준다. 가짜 키만 쓴다."""

from __future__ import annotations

from datetime import timedelta

from nsm.engine import key_watch as kw
from tests.app.test_key_expiry_c53 import _memo, _row, _today
from tests.app.test_keys_routes import KEY, KEY2, _page, _post
from tests.app.test_keys_routes import env as env  # noqa: F401  픽스처를 그대로 쓴다

URL = "/settings/keys/anthropic_api_key"
SUFFIX = "다음 작업부터 씁니다"


def test_new_date_message(env) -> None:
    d = _today(env) + timedelta(days=30)
    r = _post(env, URL, value=KEY, with_date="1", expires=d.isoformat())
    assert f"Claude 키를 저장했습니다. 만료일 {d:%m-%d}를 적었습니다. {SUFFIX}" in _page(env, r)
    assert _memo(env) == {"anthropic_api_key": d.isoformat()}


def test_blank_date_message(env) -> None:
    r = _post(env, URL, value=KEY, with_date="1", expires="")
    assert (
        "Claude 키를 저장했습니다. 만료일은 적지 않았습니다"
        f"(만료가 있는 키면 날짜만 따로 저장하세요). {SUFFIX}"
    ) in _page(env, r)
    assert _memo(env) == {}


def test_prefilled_old_date_message(env) -> None:
    old = _today(env) + timedelta(days=2)
    _post(env, URL, value=KEY, with_date="1", expires=old.isoformat())
    r = _post(env, URL, value=KEY2, with_date="1", expires=old.isoformat())
    assert (
        f"Claude 키를 저장했습니다. 옛 만료일({old:%m-%d})은 지웠습니다. "
        f"새 키에 만료일이 있으면 날짜만 따로 저장하세요. {SUFFIX}"
    ) in _page(env, r)
    assert _memo(env) == {}
    assert kw.key_banner(env["state"].repo, env["state"].clock.now(), "Asia/Seoul") is None


def test_label_and_hint(env) -> None:
    html = _page(env)
    assert "만료일(없으면 비움)" in _row(html, "Claude", "Gemini")
    assert "만료가 없는 키는 비워 두면 됩니다" in html
    assert "새 키를 저장할 때는 옛 만료일을 지우니 새 날짜를 같이 적으세요" in html


def test_env_key_date_form_unchanged(env) -> None:
    env["secrets"].env["openai_api_key"] = "fake-openai-env-key-DDDDDDDDDDDD"
    row = _row(_page(env), "OpenAI", None)
    assert "만료일(선택)" in row and 'name="value"' not in row


def test_saved_msg_branches() -> None:
    """리뷰: 문구 판단을 함수로 바로 확인(옛 날짜가 있고 칸을 비운 경우 포함)."""
    from datetime import date

    from nsm.app.keys import _saved_msg

    old, new = date(2026, 12, 12), date(2027, 1, 10)
    assert "옛 만료일(12-12)은 지웠습니다" in _saved_msg("Claude", old, old)
    assert "만료일 01-10를 적었습니다" in _saved_msg("Claude", new, old)
    assert "만료일 01-10를 적었습니다" in _saved_msg("Claude", new, None)
    assert "만료일은 적지 않았습니다" in _saved_msg("Claude", None, old)
    assert "만료일은 적지 않았습니다" in _saved_msg("Claude", None, None)
