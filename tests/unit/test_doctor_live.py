from __future__ import annotations

from types import SimpleNamespace

from nsm.config.secrets import MemorySecretStore
from nsm.doctor_live import run_live_checks


def test_live_checks_skip_without_keys() -> None:
    checks = run_live_checks(MemorySecretStore(), with_image=True)
    assert [c.status for c in checks] == ["WARN", "WARN", "WARN"]


def test_live_claude_check_reports_failure(monkeypatch) -> None:
    import anthropic

    class Boom:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=self._fail)

        def _fail(self, **kw):
            raise RuntimeError("invalid x-api-key")

    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    checks = run_live_checks(MemorySecretStore({"anthropic_api_key": "bad"}))
    assert checks[0].status == "FAIL" and "invalid" in checks[0].detail
