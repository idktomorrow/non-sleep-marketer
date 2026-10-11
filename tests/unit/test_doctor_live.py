from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nsm.config.schema import ModelSettings
from nsm.config.secrets import MemorySecretStore
from nsm.doctor import Check
from nsm.doctor_live import run_live_checks


def test_live_checks_warn_without_keys() -> None:
    checks = run_live_checks(MemorySecretStore(), with_image=True, models=ModelSettings())
    assert [c.status for c in checks] == ["WARN", "WARN", "WARN"]


@pytest.fixture
def fake_anthropic(monkeypatch):
    import anthropic

    calls = {}

    class Client:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kw):
            calls.update(kw)
            if calls.get("fail"):
                raise RuntimeError("invalid x-api-key")
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(type="text", text='{"label": "relevant", "confidence": 0.97}')
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    return calls


def test_claude_ok_uses_configured_model(fake_anthropic, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NSM_ANTHROPIC_API_KEY", raising=False)
    checks = run_live_checks(
        MemorySecretStore({"anthropic_api_key": "k"}), models=ModelSettings(classifier="claude-x")
    )
    assert (
        checks[0].status == "OK"
        and "claude-x" in checks[0].detail
        and "keyring" in checks[0].detail
    )
    assert fake_anthropic["model"] == "claude-x"


def test_claude_failure_is_fail(fake_anthropic) -> None:
    fake_anthropic["fail"] = True
    checks = run_live_checks(
        MemorySecretStore({"anthropic_api_key": "bad"}), models=ModelSettings()
    )
    assert checks[0].status == "FAIL" and "invalid" in checks[0].detail


def test_env_key_source_is_reported(fake_anthropic, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    checks = run_live_checks(MemorySecretStore({"anthropic_api_key": "k"}), models=ModelSettings())
    assert "환경 변수" in checks[0].detail


def test_optional_failures_are_warn(monkeypatch) -> None:
    import httpx

    from nsm.adapters.classifier import jev

    def bad_connect(self):
        raise RuntimeError("bad token")

    monkeypatch.setattr(jev.JevClassifier, "_db", bad_connect)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: SimpleNamespace(status_code=401, text="unauthorized", json=dict),
    )
    secrets = MemorySecretStore({"motherduck_token": "t", "openai_api_key": "o"})
    checks = run_live_checks(secrets, with_image=True, models=ModelSettings())
    assert [c.status for c in checks[1:]] == ["WARN", "WARN"]


def test_openai_ok(monkeypatch, tmp_path) -> None:
    from nsm.adapters.generator import image_api

    b64 = base64.b64encode(b"png").decode()
    monkeypatch.setattr(
        image_api.httpx,
        "post",
        lambda *a, **k: SimpleNamespace(
            status_code=200, text="", json=lambda: {"data": [{"b64_json": b64}]}
        ),
    )
    checks = run_live_checks(
        MemorySecretStore({"openai_api_key": "o"}), with_image=True, models=ModelSettings()
    )
    assert checks[2].status == "OK"


def test_cli_flags(monkeypatch) -> None:
    import nsm.doctor
    import nsm.doctor_live
    from nsm.cli import app

    seen = {}
    monkeypatch.setattr(nsm.doctor, "run_checks", lambda: [Check("offline", "OK", "")])

    def fake_live(with_image=False, **kw):
        seen["with_image"] = with_image
        return [Check("live", "OK", "")]

    monkeypatch.setattr(nsm.doctor_live, "run_live_checks", fake_live)
    r = CliRunner().invoke(app, ["doctor", "--with-image"])  # --with-image는 --live를 포함
    assert "live" in r.output and seen["with_image"] is True
    r = CliRunner().invoke(app, ["doctor", "--live-only"])
    assert "offline" not in r.output and "live" in r.output
