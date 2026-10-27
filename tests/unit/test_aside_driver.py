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
        self.stderr = ""
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


def test_run_carries_failure_evidence_and_script_collects_it(monkeypatch, tmp_path: Path) -> None:
    """E1: 스크립트가 실패하면 탭을 닫기 전에 증거를 모으고, 드라이버가 AsideError.evidence로 넘긴다."""
    r = A.AsideRepl(exe=tmp_path / "aside.exe")
    ev = {"url": "https://x", "dialogs": 1, "labels": ["닫기"], "text": "t", "screenshot": "AA=="}
    out = A.MARKER + json.dumps(
        {"ok": False, "code": "script", "error": "버튼 없음", "evidence": ev}
    )
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **kw: _Proc(out))
    with pytest.raises(A.AsideError) as e:
        r.run("x")
    assert e.value.code == "script" and e.value.evidence == ev
    script = A.build_script("return 1;", {})
    # 증거 수집은 catch 안(탭을 닫는 finally보다 먼저)이고, 쿠키·저장소는 읽지 않는다
    assert script.index("nsmEvidence()") < script.index("closeTab")
    assert "cookie" not in script.lower() and "localStorage" not in script
