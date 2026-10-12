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


def test_live_check_costs_are_recorded_with_doctor_ref(fake_anthropic, monkeypatch) -> None:
    """배치 Q1: doctor --live 호출 비용도 오늘 비용(costs)에 들어간다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NSM_ANTHROPIC_API_KEY", raising=False)
    recorded = []

    class Sink:
        def record_cost(self, kind, provider, usd, ref=""):
            recorded.append((kind, provider, usd, ref))

    from nsm.doctor_live import _Tagged

    run_live_checks(
        MemorySecretStore({"anthropic_api_key": "k"}), models=ModelSettings(), sink=_Tagged(Sink())
    )
    assert recorded and recorded[0][1] == "anthropic" and recorded[0][2] > 0
    assert recorded[0][3].startswith("doctor")


def test_cost_sink_does_not_create_db_before_init(nsm_home) -> None:
    from nsm.config import paths
    from nsm.doctor_live import cost_sink

    with cost_sink() as sink:
        assert sink is None
    assert not paths.db_path().exists()


def test_cost_sink_writes_to_configured_db(nsm_home) -> None:
    from nsm.config import paths
    from nsm.doctor_live import cost_sink
    from nsm.store.db import Database

    Database(paths.db_path()).close()  # nsm init 이후처럼 DB가 있음

    with cost_sink() as sink:
        assert sink is not None
        sink.record_cost("classifier", "anthropic", 0.001, "x")
    db = Database(paths.db_path())
    try:
        rows = [tuple(r) for r in db.execute("SELECT provider, ref FROM costs")]
        assert rows == [("anthropic", "doctor:x")]
    finally:
        db.close()


def test_cost_sink_is_none_when_db_is_locked(nsm_home, monkeypatch) -> None:
    import nsm.store.db as dbmod
    from nsm.doctor_live import cost_sink

    def locked(*a, **k):
        raise RuntimeError("IO Error: Could not set lock on file")

    monkeypatch.setattr(dbmod, "Database", locked)
    with cost_sink() as sink:
        assert sink is None
