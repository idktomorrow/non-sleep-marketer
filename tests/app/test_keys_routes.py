"""C35: 설정 화면의 API 키 칸. 등록·덮어쓰기·삭제·확인, 값은 어디에도 나오지 않는다(PR #101 S13에서 가져와 고침)."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from nsm.app.keys import check_key, check_text
from nsm.app.server import AppState, create_app
from nsm.config.secrets import MemorySecretStore
from nsm.store.repo import Repository
from tests.app.test_dashboard import _run_now

# 가짜 값(진짜 키 아님). 페이지·로그·파일에 이 문자열이 나오면 안 된다
KEY = "fake-claude-key-AAAAAAAAAAAAAAAAAAAAAAAA"
KEY2 = "fake-claude-key-BBBBBBBBBBBBBBBBBBBBBBBB"
GEMINI = "fake-gemini-key-CCCCCCCCCCCCCCCCCCCCCC"


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    secrets = MemorySecretStore({"gemini_api_key": GEMINI})
    holder = {"cfg": app_config}
    changed: list[int] = []
    checked: list[tuple[str, str | None]] = []

    def fake_check(name: str, value: str | None):
        checked.append((name, value))
        return ("ok", 200)

    state = AppState(
        repo=Repository(db, clock),
        clock=clock,
        get_config=lambda: holder["cfg"],
        set_config=lambda c: holder.__setitem__("cfg", c),
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
        secrets=secrets,
        on_secrets_changed=lambda: changed.append(1),
        check_key=fake_check,
    )
    return {
        "client": TestClient(create_app(state)),
        "state": state,
        "secrets": secrets,
        "changed": changed,
        "checked": checked,
        "holder": holder,
    }


def _post(env, url, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, follow_redirects=False
    )


def _page(env, r=None) -> str:
    url = r.headers["location"] if r is not None else "/settings"
    return env["client"].get(url).text


def test_page_shows_presence_only_in_our_order(env) -> None:
    html = _page(env)
    assert 'id="api-keys"' in html and "등록됨" in html and "없음" in html
    assert GEMINI not in html and GEMINI[-4:] not in html  # 끝 4자리도 안 보인다
    c, g, o = html.index("<b>Claude</b>"), html.index("<b>Gemini</b>"), html.index("<b>OpenAI</b>")
    assert c < g < o
    assert "원고·답글을 씁니다. 필수" in html and "무료 구간" in html and "기본은 꺼짐" in html
    assert "하나면" not in html  # "OpenAI 키 하나면 전부" 같은 문구 없음
    assert 'type="password"' in html and 'autocomplete="off"' in html


def test_register_auto_checks_then_overwrite_and_delete(env) -> None:
    r = _post(env, "/settings/keys/anthropic_api_key", value=KEY)
    assert r.status_code == 303
    assert env["secrets"].get("anthropic_api_key") == KEY and env["changed"] == [1]
    assert env["checked"] == [("anthropic_api_key", KEY)]  # 저장 직후 자동 확인
    html = _page(env, r)
    assert "Claude 키를 저장했습니다" in html and "연결됨" in html
    assert KEY not in html and KEY[-4:] not in html
    _post(env, "/settings/keys/anthropic_api_key", value=KEY2)  # 덮어쓰기
    assert env["secrets"].get("anthropic_api_key") == KEY2 and env["changed"] == [1, 1]
    r = _post(env, "/settings/keys/anthropic_api_key/delete")
    assert env["secrets"].get("anthropic_api_key") is None and env["changed"] == [1, 1, 1]
    assert "Claude 키를 지웠습니다" in _page(env, r)


@pytest.mark.parametrize(
    "value",
    ["short-key", "fake key with spaces AAAAAAAAAAAA", " " * 30],
    ids=["short", "space", "blank"],
)
def test_bad_format_is_rejected(env, value: str) -> None:
    r = _post(env, "/settings/keys/anthropic_api_key", value=value)
    assert "키 형식이 아닙니다" in _page(env, r)
    assert env["secrets"].get("anthropic_api_key") is None and env["changed"] == []


def test_requests_without_csrf_are_rejected(env) -> None:
    c = env["client"]
    assert c.post("/settings/keys/anthropic_api_key", data={"value": KEY}).status_code == 403
    assert c.post("/settings/keys/gemini_api_key/delete").status_code == 403
    assert c.post("/settings/keys/gemini_api_key/check").status_code == 403
    assert env["secrets"].get("anthropic_api_key") is None
    assert env["secrets"].get("gemini_api_key") == GEMINI


def test_unknown_key_name_is_400(env) -> None:
    assert _post(env, "/settings/keys/aws_secret", value=KEY).status_code == 400


def test_env_key_cannot_be_deleted_and_says_so(env) -> None:
    env["secrets"].env["openai_api_key"] = "fake-openai-env-key-DDDDDDDDDDDD"
    assert "환경 변수 키라 여기서는 못 지움" in _page(env)
    r = _post(env, "/settings/keys/openai_api_key/delete")
    assert "환경 변수 키라 여기서는 못 지웁니다" in _page(env, r)
    assert env["secrets"].get("openai_api_key") and env["changed"] == []


def test_check_button_shows_result_on_that_row(env) -> None:
    env["state"].check_key = lambda n, v: ("limit", 429)
    r = _post(env, "/settings/keys/gemini_api_key/check")
    html = _page(env, r)
    assert "키는 맞지만 한도·결제 확인 필요(HTTP 429)" in html
    row = html[html.index("<b>Gemini</b>") : html.index("<b>OpenAI</b>")]
    assert "key-check" in row


def test_key_never_reaches_config_db_logs_or_html(env, caplog, tmp_path: Path) -> None:
    from nsm.config.store import save_config

    caplog.set_level(logging.DEBUG)
    _post(env, "/settings/keys/anthropic_api_key", value=KEY)
    _post(env, "/settings/keys/anthropic_api_key/check")
    cfg_file = tmp_path / "config.yaml"
    save_config(env["holder"]["cfg"], cfg_file)
    assert KEY not in cfg_file.read_text(encoding="utf-8")
    db = env["state"].repo.db
    for (table,) in db.execute("SELECT table_name FROM information_schema.tables"):
        for row in db.execute(f'SELECT * FROM "{table}"'):
            assert KEY not in repr(row), table
    assert KEY not in caplog.text
    assert KEY not in _page(env)


def test_no_secrets_store_hides_section(db, clock, app_config, tmp_path: Path) -> None:
    state = AppState(
        repo=Repository(db, clock),
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
    )
    c = TestClient(create_app(state))
    assert 'id="api-keys"' not in c.get("/settings").text
    r = c.post("/settings/keys/anthropic_api_key", data={"csrf": state.csrf_token, "value": KEY})
    assert r.status_code == 404


# ------------------------------------------------------------------ 키 확인(가짜 HTTP 응답)


def _transport(status: int, body: str, seen: list) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("name", "status", "body", "expected"),
    [
        ("anthropic_api_key", 200, '{"data": []}', "ok"),
        ("anthropic_api_key", 401, '{"type": "authentication_error"}', "invalid"),
        ("openai_api_key", 403, "{}", "invalid"),
        (
            "gemini_api_key",
            400,
            '{"error": {"details": [{"reason": "API_KEY_INVALID"}]}}',
            "invalid",
        ),
        ("openai_api_key", 429, '{"error": {"code": "insufficient_quota"}}', "limit"),
        ("gemini_api_key", 400, '{"error": {"status": "FAILED_PRECONDITION"}}', "limit"),
        ("anthropic_api_key", 503, "{}", "network"),
    ],
)
def test_check_key_classifies_responses(name, status, body, expected) -> None:
    seen: list[httpx.Request] = []
    assert check_key(name, KEY, transport=_transport(status, body, seen)) == (expected, status)
    req = seen[0]
    assert req.method == "GET" and req.url.path.endswith("/models")  # 생성 없이 목록 조회만
    assert KEY not in str(req.url)  # 키는 주소가 아니라 헤더로


def test_check_key_network_failure_and_missing() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=req)

    t = httpx.MockTransport(boom)
    assert check_key("openai_api_key", KEY, transport=t) == ("network", None)
    assert check_key("openai_api_key", None) == ("missing", None)


def test_check_text_never_includes_response_body() -> None:
    assert check_text("invalid", 401) == "키가 틀렸거나 만료됨"
    assert check_text("limit", 429) == "키는 맞지만 한도·결제 확인 필요(HTTP 429)"
    assert check_text("network", None) == "연결 실패(네트워크·시간 초과)"
