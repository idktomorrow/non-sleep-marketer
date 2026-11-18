"""폴더 선택 창이 브라우저 앞에 뜨게(배치 C51) + 설정 바로가기 "백업" 칩 + 쉬운 문구.

진짜 PowerShell은 부르지 않는다(실제 창 확인은 devlog의 수동 확인).
"""

from __future__ import annotations

import re

import pytest

from nsm.app import folder_picker as F
from nsm.app.ko import FOLDER_PICKER
from tests.app.test_dashboard import _post, env  # noqa: F401
from tests.app.test_folder_picker_c50 import FakePopen, FakeRunner


@pytest.fixture(autouse=True)
def no_real_powershell(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("진짜 PowerShell을 불렀습니다")

    monkeypatch.setattr(F.subprocess, "run", forbidden)
    monkeypatch.setattr(F.subprocess, "Popen", forbidden)


def _order(s: str, *parts: str) -> list[int]:
    idx = [s.index(p) for p in parts]
    assert idx == sorted(idx), parts
    return idx


def test_owner_form_is_shown_centered_and_activated_before_dialog() -> None:
    s = F.picker_script("t", r"C:\x")
    assert "Add-Type -AssemblyName System.Drawing" in s
    for part in (
        "$o.FormBorderStyle = 'None'",
        "$o.ShowInTaskbar = $false",
        "$o.TopMost = $true",
        "$o.Opacity = 0",
        "$o.Size = New-Object System.Drawing.Size(1, 1)",
        "$o.StartPosition = 'CenterScreen'",  # 화면 밖이면 대화 상자도 화면 밖으로 간다
    ):
        assert part in s
    # 띄우고 → 최소화·복원(포그라운드 잠금 우회) → Activate → 대화 상자 → finally에서 닫기
    _order(
        s,
        "$o.StartPosition = 'CenterScreen'",
        "$o.Show();",
        "$o.WindowState = 'Minimized'; $o.WindowState = 'Normal'; $o.Activate();",
        "$d.ShowDialog($o)",
        "finally {",
        "$o.Close(); $o.Dispose()",
        "exit $code",
    )
    assert "SendKeys" not in s  # 최소화·복원으로 충분해 Alt 키 보내기는 넣지 않았다(ADR 0075)


def test_structure_kept_sta_encoded_utf8_and_exit_codes() -> None:
    runner = FakeRunner("")
    assert F.pick_folder("t", runner=runner) is None  # 취소 = None
    args = runner.calls[0][0]
    assert args[1] == "-STA" and args[-2] == "-EncodedCommand"
    s = runner.script()
    assert "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false" in s
    assert "else { $code = 3 }" in s and "$code = 1 }" in s
    # exit는 finally 뒤 한 번뿐: 주인 폼을 닫지 못한 채 끝나지 않게
    assert re.findall(r"\bexit\b", s) == ["exit"] and s.endswith("exit $code")


def test_exit_code_3_is_still_not_folder(monkeypatch) -> None:
    monkeypatch.setattr(F.sys, "platform", "win32")
    monkeypatch.setattr(F.subprocess, "Popen", FakePopen(3))
    with pytest.raises(F.PickerNotFolder):
        F.pick_folder("t")


def test_quotes_still_escaped_in_new_script() -> None:
    s = F.picker_script("제목 '따옴표' ‘굽은’", "C:\\x'; $(calc)")
    assert "$d.Description = '제목 ''따옴표'' ‘‘굽은’’'; " in s
    assert "$d.SelectedPath = 'C:\\x''; $(calc)'; " in s


def test_new_dialog_title() -> None:
    assert FOLDER_PICKER["title"] == "백업 파일을 복사해 둘 폴더를 선택하세요"
    runner = FakeRunner("")
    F.pick_folder(FOLDER_PICKER["title"], runner=runner)
    assert "$d.Description = '백업 파일을 복사해 둘 폴더를 선택하세요'" in runner.script()


def _jump(html: str) -> str:
    return html[
        html.index('<nav class="jump') : html.index("</nav>", html.index('<nav class="jump'))
    ]


def test_backup_chip_only_in_advanced_mode(env) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    nav = _jump(env["client"].get("/settings").text)
    assert '<a href="#backup">백업</a>' in nav
    _order(nav, 'href="#ops"', 'href="#backup"', 'href="#alerts"')  # 절 순서대로
    _post(env, "/ui-mode", mode="simple")
    html = env["client"].get("/settings").text
    assert 'href="#backup"' not in _jump(html)  # 숨은 절로 가는 칩은 만들지 않는다
    assert 'href="#ops"' in _jump(html) and 'href="#alerts"' in _jump(html)


def test_backup_hint_is_plain(env) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/settings").text
    assert (
        "매일 새벽에 만드는 백업 파일을 이 폴더에도 복사해 둡니다. 구글 드라이브나 OneDrive 폴더를 고르면 "
        "클라우드에도 남습니다. 기본 백업과 같은 개수만 남기고 옛 파일은 지우니 PC 두 대가 같은 폴더를 "
        "쓰지 마세요. 비우면 끕니다"
    ) in html
    assert "한 부 더" not in html
