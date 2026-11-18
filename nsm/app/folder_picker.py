"""이 PC의 Windows 폴더 선택 창을 띄워 고른 경로를 돌려준다(배치 C50, ADR 0075).

브라우저의 폴더 선택은 전체 경로를 페이지에 주지 않는다. 대시보드는 127.0.0.1 전용이라, 서버(nsm run 프로세스)가
같은 PC에서 선택 창을 띄우면 그 창을 보는 사람이 곧 대시보드를 쓰는 사람이다.

알림(nsm/adapters/notify/windows.py)처럼 PowerShell을 -EncodedCommand로 부르고, 사용자 값(title·initial)은
작은따옴표 리터럴 안에만 넣는다. 실행기는 주입할 수 있어 테스트는 진짜 창을 띄우지 않는다.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable

from nsm.adapters.notify.windows import encode_command


class PickerUnavailable(Exception):
    """선택 창을 쓸 수 없음(Windows 아님·PowerShell 없음·시간 초과·PowerShell 오류)."""


class PickerBusy(Exception):
    """다른 선택 창이 이미 떠 있음."""


# 실행기: (명령 인자, 제한 시간 초) -> 표준 출력(UTF-8로 푼 문자열). 실패는 예외로 알린다
Runner = Callable[[list[str], float], str]

_lock = threading.Lock()  # 선택 창은 한 번에 하나만

# PowerShell은 작은따옴표 문자열에서 ' 말고도 이 문자들을 따옴표로 본다. 모두 두 번 써서 글자로 만든다
_PS_QUOTES = "'‘’‚‛"


def ps_literal(value: str) -> str:
    """PowerShell 작은따옴표 문자열 리터럴. 안에서는 변수·$()가 해석되지 않는다."""
    out = "".join(c + c if c in _PS_QUOTES else c for c in value.replace("\x00", ""))
    return f"'{out}'"


def picker_script(title: str, initial: str = "") -> str:
    """FolderBrowserDialog를 띄우는 스크립트. OK면 경로 한 줄, 취소면 아무것도 출력하지 않는다."""
    selected = f"$d.SelectedPath = {ps_literal(initial)}; " if initial else ""
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::EnableVisualStyles(); "
        # 브라우저 뒤에 숨지 않게: 맨 위에 뜨는(보이지 않는) 폼을 주인으로 넘긴다
        "$o = New-Object System.Windows.Forms.Form; $o.TopMost = $true; $o.ShowInTaskbar = $false; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.Description = {ps_literal(title)}; "
        "$d.ShowNewFolderButton = $true; "
        f"{selected}"
        "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.WriteLine($d.SelectedPath) }; "
        "$d.Dispose(); $o.Dispose()"
    )


def powershell_runner(args: list[str], timeout_s: float) -> str:
    """진짜 실행기. 테스트에서는 쓰지 않는다."""
    if sys.platform != "win32":
        raise PickerUnavailable("Windows가 아님")
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout_s,  # 시간이 지나면 PowerShell을 끝내 창도 닫힌다
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 콘솔만 숨김, 선택 창은 보임
        )
    except FileNotFoundError as e:
        raise PickerUnavailable("PowerShell 없음") from e
    except subprocess.TimeoutExpired as e:
        raise PickerUnavailable("시간 초과") from e
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()[:300]
        raise PickerUnavailable(f"PowerShell 오류(코드 {r.returncode}): {err}")
    return r.stdout.decode("utf-8", "replace")


def pick_folder(
    title: str, initial: str = "", timeout_s: float = 300, runner: Runner | None = None
) -> str | None:
    """선택 창을 띄우고 고른 폴더 경로를 돌려준다. 취소면 None.

    PickerBusy: 다른 창이 떠 있음(기다리지 않고 바로). PickerUnavailable: 창을 쓸 수 없음.
    """
    if not _lock.acquire(blocking=False):
        raise PickerBusy
    try:
        args = [
            "powershell.exe",
            "-STA",  # Windows Forms 대화 상자는 STA 스레드가 필요하다
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encode_command(picker_script(title, initial)),
        ]
        try:
            out = (runner or powershell_runner)(args, timeout_s)
        except PickerUnavailable:
            raise
        except Exception as e:  # noqa: BLE001  실행기의 어떤 실패도 "쓸 수 없음" 하나로
            raise PickerUnavailable(f"{type(e).__name__}: {e}") from e
        # 앞뒤 공백·줄바꿈(\r\n)을 떼고, 혹시 다른 줄이 섞이면 마지막 줄만
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    finally:
        _lock.release()
