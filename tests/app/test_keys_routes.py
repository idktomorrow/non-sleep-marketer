"""API 키 화면(S13 제안): 등록 여부만 보이고 값은 어디에도 나오지 않는다. 등록·삭제 뒤 어댑터 갱신 콜백."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.config.secrets import MemorySecretStore
from nsm.store.repo import Repository
from tests.app.test_dashboard import _run_now

KEY = "sk-live-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    secrets = MemorySecretStore({"gemini_api_key": "g-key-000000000000000000"})
    holder = {"cfg": app_config}
    changed: list[int] = []
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
    )
    client = TestClient(create_app(state))
    return {"client": client, "state": state, "secrets": secrets, "changed": changed}


def _post(env, url, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, follow_redirects=False
    )


def test_page_shows_presence_only(env) -> None:
    html = env["client"].get("/settings").text
    assert "API 키" in html and "등록됨" in html and "없음" in html
    assert "g-key-0000" not in html and 'name="text_provider"' in html
    assert "OpenAI 키 하나면" in html


def test_register_then_delete(env) -> None:
    r = _post(env, "/settings/keys/openai_api_key", value=KEY)
    assert r.status_code == 303 and "key_msg" in r.headers["location"]
    assert env["secrets"].get("openai_api_key") == KEY and env["changed"] == [1]
    html = env["client"].get("/settings").text
    assert KEY not in html and KEY[:12] not in html
    r = _post(env, "/settings/keys/openai_api_key/delete")
    assert r.status_code == 303 and env["secrets"].get("openai_api_key") is None
    assert env["changed"] == [1, 1]


def test_rejects_bad_input_and_unknown_key(env) -> None:
    r = _post(env, "/settings/keys/openai_api_key", value="short")
    assert "key_error" in r.headers["location"] and env["secrets"].get("openai_api_key") is None
    assert _post(env, "/settings/keys/nope", value=KEY).status_code == 400
    assert (
        env["client"].post("/settings/keys/openai_api_key", data={"value": KEY}).status_code == 403
    )


def test_hidden_without_secret_store(db, clock, app_config, tmp_path) -> None:
    state = AppState(
        repo=Repository(db, clock), clock=clock, get_config=lambda: app_config,
        set_config=lambda c: None, submit=_run_now, adapters={}, media_dir=tmp_path,
    )  # fmt: skip
    client = TestClient(create_app(state))
    assert 'id="ai-keys"' not in client.get("/settings").text
    assert (
        client.post(
            "/settings/keys/openai_api_key", data={"csrf": state.csrf_token, "value": KEY}
        ).status_code
        == 404
    )


def test_settings_form_saves_provider_and_models(env) -> None:
    from nsm.app.settings_form import SettingsError, apply_settings_form, form_values

    cfg = env["state"].get_config()
    form = {
        **form_values(cfg),
        "text_provider": "openai",
        "openai_text": "gpt-6-luna",
        "image_quality": "low",
    }
    new = apply_settings_form(cfg, form)
    assert (
        new.settings.text_provider == "openai" and new.settings.models.openai_text == "gpt-6-luna"
    )
    assert new.settings.models.image_quality == "low"
    with pytest.raises(SettingsError):
        apply_settings_form(cfg, {**form, "text_provider": "gemini"})
    with pytest.raises(SettingsError):
        apply_settings_form(cfg, {**form, "openai_text": "gpt 6"})
