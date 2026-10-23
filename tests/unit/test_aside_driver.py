"""Aside repl 실행기: 값 전달·결과 해석·파일 서버·오류 정리 (실제 Aside 없이)."""

from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nsm.adapters.drivers import aside as A


def _payload(script: str) -> dict:
    b64 = script.split("Buffer.from(", 1)[1].split(",", 1)[0]
    return json.loads(base64.b64decode(json.loads(b64)).decode("utf-8"))


def test_values_go_in_as_data_not_code() -> None:
    tricky = "따옴표 \" ' `bt` ${x} </script> %s \\ \n  ☕"
    script = A.build_script("return ARGS.t;", {"t": tricky})
    assert tricky not in script  # 문구가 JS 소스에 그대로 들어가지 않는다
    assert _payload(script)["t"] == tricky


def test_body_with_percent_is_kept() -> None:
    assert "50% off" in A.build_script("const s = '50% off'; return s;", {})


def test_parse_output_reads_last_marker_line() -> None:
    out = "noise\n\x1b[2m" + A.MARKER + '{"ok": true, "data": 1}\x1b[0m\nmore\n'
    assert A.parse_output(out) == {"ok": True, "data": 1}
    with pytest.raises(A.AsideError) as e:
        A.parse_output("no marker here")
    assert e.value.code == "cli"


def test_scrub_removes_ansi_and_secret_like_values() -> None:
    s = A.scrub("\x1b[31mfail\x1b[0m cookie: abc123 sessionid=zzz ok")
    assert "abc123" not in s and "zzz" not in s and "\x1b" not in s and "fail" in s


def test_file_server_serves_only_listed_files(tmp_path: Path) -> None:
    f = tmp_path / "slide-01.png"
    f.write_bytes(b"\x89PNG-data")
    other = tmp_path / "secret.txt"
    other.write_text("nope")
    with A.serve_files([f]) as entries:
        url = entries[0]["url"]
        assert url.startswith("http://127.0.0.1:") and entries[0]["name"] == "f00.png"
        assert urllib.request.urlopen(url, timeout=5).read() == b"\x89PNG-data"
        base = url.rsplit("/", 2)[0]
        for bad in (f"{base}/secret.txt", url.rsplit("/", 1)[0] + "/../secret.txt"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(bad, timeout=5)
    with pytest.raises(urllib.error.URLError):  # 호출이 끝나면 닫힌다
        urllib.request.urlopen(url, timeout=2)


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def test_run_calls_cli_without_shell(monkeypatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _Proc(A.MARKER + json.dumps({"ok": True, "data": {"x": 1}}))

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    r = A.AsideRepl(exe=tmp_path / "aside.exe", account="u0")
    assert r.run("return 1;", {"a": 1}) == {"x": 1}
    assert isinstance(seen["cmd"], list) and seen["cmd"][1:4] == ["repl", "--account", "u0"]
    assert "shell" not in seen["kw"] or seen["kw"]["shell"] is False


def test_run_maps_script_errors_and_timeout(monkeypatch, tmp_path: Path) -> None:
    r = A.AsideRepl(exe=tmp_path / "aside.exe")
    monkeypatch.setattr(
        A.subprocess,
        "run",
        lambda cmd, **kw: _Proc(
            A.MARKER + json.dumps({"ok": False, "code": "login", "error": "로그인 필요"})
        ),
    )
    with pytest.raises(A.AsideError) as e:
        r.run("x")
    assert e.value.code == "login"

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(A.subprocess, "run", boom)
    with pytest.raises(A.AsideError) as e2:
        r.run("x")
    assert e2.value.code == "timeout"


def test_missing_cli_is_driver_unavailable() -> None:
    r = A.AsideRepl.__new__(A.AsideRepl)
    r.exe, r.account = None, None
    with pytest.raises(A.DriverUnavailableError):
        r.run("x")


class FakeRepl:
    def __init__(self, data) -> None:
        self.data = data
        self.calls: list[tuple[str, dict, list]] = []

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        self.calls.append((body, args or {}, list(files or [])))
        return self.data


def test_aside_collect_driver_reads_page_once() -> None:
    fake = FakeRepl(
        {"links": [{"text": "t", "href": "https://x/1", "context": "c"}], "text": "본문"}
    )
    d = A.AsideDriver(fake)
    d.goto("https://search.example/?q=1")
    assert [link.href for link in d.links()] == ["https://x/1"] and d.page_text() == "본문"
    assert len(fake.calls) == 1 and fake.calls[0][1]["url"] == "https://search.example/?q=1"


def test_find_aside_cli_accepts_unix_binary_but_not_windows_wrappers(tmp_path, monkeypatch) -> None:
    """macOS·리눅스 Aside CLI는 확장자 없는 실행 파일이다(실측: ~/.local/bin/aside). Windows는 .exe만(.cmd 거부)."""
    import os
    import shutil

    from nsm.adapters import drivers as D

    exe = tmp_path / "aside"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(os, "name", "posix")
    assert D.find_aside_cli() == exe
    cmd = tmp_path / "aside.cmd"
    cmd.write_text("@echo off")
    monkeypatch.setattr(shutil, "which", lambda name: str(cmd))
    monkeypatch.setattr(os, "name", "nt")
    assert D.find_aside_cli() is None


def test_live_adapters_use_configured_aside_account(app_config) -> None:
    """Aside 계정이 여럿이면 게시 계정이 로그인된 쪽을 쓴다(실측: 인스타그램은 u1에만 로그인)."""
    from nsm.domain.clock import SystemClock
    from nsm.runtime import make_live_adapters

    s = app_config.settings.model_copy(
        update={
            "enabled_platforms": ["instagram"],
            "accounts": {"instagram": "@cikawa21"},
            "aside_account": "u1",
        }
    )
    adapters = make_live_adapters(app_config.model_copy(update={"settings": s}), SystemClock())
    assert adapters["instagram"].repl.account == "u1"
    s2 = s.model_copy(update={"aside_account": None})
    assert (
        make_live_adapters(app_config.model_copy(update={"settings": s2}), SystemClock())[
            "instagram"
        ].repl.account
        is None
    )
