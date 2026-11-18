"""설정 화면 "폴더 고르기…" 버튼 (배치 C50). 진짜 선택 창(PowerShell)은 띄우지 않고 가짜 실행기만 쓴다."""

from __future__ import annotations

import base64
import subprocess
import threading
from pathlib import Path

import pytest

from nsm.app import folder_picker as F
from nsm.app.ko import FOLDER_PICKER
from tests.app.test_dashboard import _post, env  # noqa: F401


@pytest.fixture(autouse=True)
def no_real_powershell(monkeypatch):
    """이 파일의 어떤 테스트도 진짜 PowerShell을 부르지 않는다(부르면 바로 실패)."""

    def forbidden(*a, **kw):
        raise AssertionError("진짜 PowerShell을 불렀습니다")

    monkeypatch.setattr(F.subprocess, "run", forbidden)


class FakeRunner:
    def __init__(self, out: str = "", exc: Exception | None = None) -> None:
        self.out, self.exc = out, exc
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args: list[str], timeout_s: float) -> str:
        self.calls.append((args, timeout_s))
        if self.exc:
            raise self.exc
        return self.out

    def script(self) -> str:
        args = self.calls[-1][0]
        return base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")


def _pick(env, runner, initial: str = ""):  # noqa: F811
    env["state"].folder_runner = runner
    return _post(env, "/settings/pick-folder", initial=initial)


def test_picked_path_is_returned_trimmed(env) -> None:  # noqa: F811
    runner = FakeRunner("  C:\\Users\\me\\OneDrive\\nsm-backups \r\n")
    r = _pick(env, runner, initial="C:\\old")
    assert r.status_code == 200 and r.json() == {"path": "C:\\Users\\me\\OneDrive\\nsm-backups"}
    args, timeout = runner.calls[0]
    assert args[:5] == [
        "powershell.exe",
        "-STA",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
    ]
    assert timeout == 300
    s = runner.script()
    assert "FolderBrowserDialog" in s and "$d.ShowNewFolderButton = $true" in s
    assert "TopMost = $true" in s and "ShowDialog($o)" in s
    assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in s
    assert "$d.SelectedPath = 'C:\\old'" in s
    assert f"$d.Description = '{FOLDER_PICKER['title']}'" in s


def test_empty_output_means_cancelled(env) -> None:  # noqa: F811
    r = _pick(env, FakeRunner("\r\n"))
    assert r.status_code == 200 and r.json() == {"cancelled": True}


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired("powershell.exe", 300),
        FileNotFoundError("powershell.exe"),
        F.PickerUnavailable("Windows가 아님"),
        RuntimeError("뜻밖"),
    ],
)
def test_failure_is_200_with_korean_error(env, exc) -> None:  # noqa: F811
    # 성공·취소·오류 모두 200 JSON: 화면 스크립트가 error 문구를 그대로 보여 준다(라우트 주석)
    r = _pick(env, FakeRunner(exc=exc))
    assert r.status_code == 200 and r.json() == {"error": FOLDER_PICKER["unavailable"]}


def test_without_csrf_is_rejected(env) -> None:  # noqa: F811
    runner = FakeRunner("C:\\x")
    env["state"].folder_runner = runner
    r = env["client"].post("/settings/pick-folder", data={"initial": ""})
    assert r.status_code == 403 and runner.calls == []
    r = env["client"].post("/settings/pick-folder", data={"csrf": "wrong", "initial": ""})
    assert r.status_code == 403 and runner.calls == []


def test_second_request_while_open_is_busy(env) -> None:  # noqa: F811
    opened, close = threading.Event(), threading.Event()

    def slow(args, timeout_s):
        opened.set()
        close.wait(5)
        return "C:\\first\n"

    got: list = []
    t = threading.Thread(target=lambda: got.append(F.pick_folder("t", runner=slow)))
    t.start()
    try:
        assert opened.wait(5)
        second = FakeRunner("C:\\second")
        r = _pick(env, second)
        assert r.status_code == 200 and r.json() == {"error": FOLDER_PICKER["busy"], "busy": True}
        assert second.calls == []  # 두 번째 창은 띄우지 않음
    finally:
        close.set()
        t.join(5)
    assert got == ["C:\\first"]
    assert _pick(env, FakeRunner("C:\\after")).json() == {"path": "C:\\after"}  # 잠금이 풀림


def test_lock_is_released_after_failure() -> None:
    with pytest.raises(F.PickerUnavailable):
        F.pick_folder("t", runner=FakeRunner(exc=OSError("x")))
    assert F.pick_folder("t", runner=FakeRunner("C:\\ok")) == "C:\\ok"


def test_quotes_in_title_and_initial_stay_inside_literal() -> None:
    assert F.ps_literal("it's") == "'it''s'"
    assert F.ps_literal("a\u2019b") == "'a\u2019\u2019b'"  # PowerShell은 ’도 따옴표로 본다
    runner = FakeRunner("")
    evil = "C:\\x'; Remove-Item C:\\ -Recurse; '$(calc)\u2018"
    F.pick_folder("제목 '따옴표' $env:USERNAME", evil, runner=runner)
    s = runner.script()
    assert "$d.Description = '제목 ''따옴표'' $env:USERNAME'; " in s
    assert "$d.SelectedPath = 'C:\\x''; Remove-Item C:\\ -Recurse; ''$(calc)\u2018\u2018'; " in s


def test_no_initial_leaves_selected_path_unset() -> None:
    runner = FakeRunner("")
    F.pick_folder("t", "", runner=runner)
    assert "SelectedPath =" not in runner.script()


def test_initial_from_form_is_cleaned(env) -> None:  # noqa: F811
    runner = FakeRunner("")
    _pick(env, runner, initial='  "D:\\Drive\\nsm"  ')
    assert "$d.SelectedPath = 'D:\\Drive\\nsm'; " in runner.script()


def test_default_runner_off_windows_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(F.sys, "platform", "linux")
    with pytest.raises(F.PickerUnavailable):
        F.pick_folder("t")  # 주입하지 않으면 진짜 실행기: Windows가 아니면 PowerShell 전에 멈춤


def test_settings_page_has_button_static_js_and_home_placeholder(env, monkeypatch, tmp_path):  # noqa: F811
    home = tmp_path / "home" / "홍길동"
    monkeypatch.setattr(Path, "home", lambda: home)
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/settings").text
    assert 'id="pick-folder"' in html and 'type="button"' in html and "폴더 고르기…" in html
    assert "/static/settings.js" in html
    assert f'placeholder="{home / "OneDrive" / "nsm-backups"}"' in html
    assert "이름\\OneDrive" not in html
    assert "홍길동" not in env["holder"]["cfg"].settings.backup_copy_dir  # 화면에만, 저장 안 됨
