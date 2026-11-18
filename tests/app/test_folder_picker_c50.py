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
    monkeypatch.setattr(F.subprocess, "Popen", forbidden)


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
    assert "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false" in s  # BOM 없이
    # 창을 띄우다 실패하면 "취소"로 보이지 않게 오류로 끝낸다(리뷰 지적)
    assert s.startswith("$ErrorActionPreference = 'Stop'; try { ")
    assert s.endswith("catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
    assert "else { exit 3 }" in s  # OK인데 실제 폴더가 아님
    assert "$d.SelectedPath = 'C:\\old'" in s
    assert f"$d.Description = '{FOLDER_PICKER['title']}'" in s


def test_empty_output_means_cancelled(env) -> None:  # noqa: F811
    r = _pick(env, FakeRunner("\r\n"))
    assert r.status_code == 200 and r.json() == {"cancelled": True}


@pytest.mark.parametrize(
    "exc",
    [
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


@pytest.mark.parametrize(
    ("exc", "key"),
    [
        (subprocess.TimeoutExpired("powershell.exe", 300), "timeout"),
        (F.PickerTimeout("시간 초과"), "timeout"),
        (F.PickerNotFolder(), "not_folder"),
    ],
)
def test_timeout_and_not_folder_have_own_message(env, exc, key) -> None:  # noqa: F811
    # 시간 초과는 창이 떴던 것: "열 수 없음"이 아니라 "닫았으니 다시"(리뷰 지적)
    r = _pick(env, FakeRunner(exc=exc))
    assert r.status_code == 200 and r.json() == {"error": FOLDER_PICKER[key]}


def test_bom_is_stripped_from_path(env) -> None:  # noqa: F811
    r = _pick(env, FakeRunner("\ufeff" + r"C:\Drive\nsm" + "\r\n"))
    assert r.json() == {"path": r"C:\Drive\nsm"}


class FakePopen:
    """진짜 실행기(powershell_runner) 시험용 가짜 프로세스."""

    def __init__(self, code=0, out=b"", err=b"", hang=False) -> None:
        self.returncode, self.out, self.err, self.hang = code, out, err, hang
        self.killed = threading.Event()

    def __call__(self, args, **kw):
        self.args = args
        return self

    def communicate(self, timeout=None):
        if self.hang and not self.killed.is_set():
            if timeout is not None and not self.killed.wait(timeout):
                raise subprocess.TimeoutExpired("powershell.exe", timeout)
            self.returncode = 1
        return self.out, self.err

    def poll(self):
        return None if self.hang and not self.killed.is_set() else self.returncode

    def kill(self):
        self.killed.set()


@pytest.fixture
def fake_windows(monkeypatch):
    monkeypatch.setattr(F.sys, "platform", "win32")

    def use(p: FakePopen) -> FakePopen:
        monkeypatch.setattr(F.subprocess, "Popen", p)
        return p

    return use


def test_real_runner_maps_exit_codes(fake_windows) -> None:
    fake_windows(FakePopen(0, ("\ufeff" + r"D:\내 드라이브\nsm" + "\r\n").encode()))
    assert F.pick_folder("t") == r"D:\내 드라이브\nsm"  # UTF-8(BOM이 와도 뗌)
    fake_windows(FakePopen(0, b""))
    assert F.pick_folder("t") is None
    fake_windows(FakePopen(3))
    with pytest.raises(F.PickerNotFolder):
        F.pick_folder("t")
    fake_windows(FakePopen(1, err="대화형 세션이 아닙니다".encode()))
    with pytest.raises(F.PickerUnavailable, match="대화형 세션"):
        F.pick_folder("t")


def test_real_runner_timeout_kills_powershell(fake_windows) -> None:
    p = fake_windows(FakePopen(hang=True))
    with pytest.raises(F.PickerTimeout):
        F.pick_folder("t", timeout_s=0.05)
    assert p.killed.is_set()


def test_cancel_picker_closes_open_dialog_on_shutdown(fake_windows) -> None:
    """nsm run을 끌 때(Ctrl+C) 떠 있는 창을 닫아, 요청이 5분까지 종료를 붙잡지 않는다(리뷰 지적)."""
    p = fake_windows(FakePopen(hang=True))
    got: list = []

    def run():
        try:
            F.pick_folder("t")
        except F.PickerUnavailable as e:
            got.append(e)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if F._proc is not None:
            break
        threading.Event().wait(0.01)
    F.cancel_picker()
    t.join(5)
    assert not t.is_alive() and p.killed.is_set() and len(got) == 1
    assert F._proc is None
    F.cancel_picker()  # 떠 있는 창이 없으면 아무것도 안 함


def test_toast_script_doubles_curly_quotes_too() -> None:
    """알림도 같은 ps_literal을 쓴다: 뉴스 제목의 ‘먼작귀’ 같은 굽은 따옴표가 리터럴을 끝내지 않게(리뷰 지적)."""
    from nsm.adapters.notify import windows as W

    s = W.toast_script("‘먼작귀’ 팝콘통", r"’; Remove-Item C:\ -Recurse; ’")
    lit = s[s.index("LoadXml('") + len("LoadXml(") : s.index("); [Windows")]
    assert lit == W.ps_literal(lit[1:-1].replace("‘‘", "‘").replace("’’", "’"))
    assert "‘‘먼작귀’’" in lit and "’’; Remove-Item" in lit
