"""Windows 알림(N1): 알림 문구가 PowerShell 코드로 해석되지 않는지, 켜기/끄기를 따르는지."""

from __future__ import annotations

import base64

from nsm.adapters.notify import windows as W


def test_toast_script_keeps_user_text_inert() -> None:
    nasty = "원고 '치이카와' <b>&</b> $(Remove-Item C:/x) ]]> \"@"
    s = W.toast_script(nasty, "본문 $env:USERNAME", "http://127.0.0.1:8765")
    # 사용자 문구는 XML 이스케이프 후 작은따옴표 문자열 안에만 있다
    lit = s[s.index("LoadXml('") + len("LoadXml('") : s.index("'); [Windows")]
    assert "''치이카와''" in lit  # 작은따옴표는 두 번(문자열이 끝나지 않음)
    assert "<b>" not in lit and "&lt;b&gt;" in lit and "&amp;" in lit
    assert "$(Remove-Item" in lit  # 작은따옴표 문자열 안이라 실행되지 않는 글자일 뿐
    assert s.count("LoadXml('") == 1 and "CreateToastNotifier('NonSleepMarketer')" in s
    assert 'launch="http://127.0.0.1:8765"' in lit


def test_encoded_command_round_trips() -> None:
    s = W.toast_script("제목", "본문")
    assert base64.b64decode(W.encode_command(s)).decode("utf-16-le") == s


def test_notifier_respects_switch_and_dedupes(monkeypatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(W.sys, "platform", "win32")

    class NoThread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(W.threading, "Thread", NoThread)
    on = {"v": True}
    n = W.DesktopNotifier(enabled=lambda: on["v"])
    monkeypatch.setattr(n, "_send", lambda t, b: sent.append((t, b)))
    n.notify("검토 대기", "2건", "info")
    n.notify("검토 대기", "2건", "info")  # 60초 안 같은 제목은 한 번만
    on["v"] = False
    n.notify("다른 알림", "꺼짐", "info")
    assert sent == [("검토 대기", "2건")]
