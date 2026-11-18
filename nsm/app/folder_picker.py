"""이 PC의 Windows 폴더 선택 창을 띄워 고른 경로를 돌려준다(배치 C50, ADR 0075).

브라우저의 폴더 선택은 전체 경로를 페이지에 주지 않는다. 대시보드는 127.0.0.1 전용이라, 서버(nsm run 프로세스)가
같은 PC에서 선택 창을 띄우면 그 창을 보는 사람이 곧 대시보드를 쓰는 사람이다.

알림(nsm/adapters/notify/windows.py)처럼 PowerShell을 -EncodedCommand로 부르고(선택 창은 STA가 필요해 -STA 추가),
사용자 값(title·initial)은 같은 `ps_literal`로 작은따옴표 리터럴 안에만 넣는다. 실행기는 주입할 수 있어 테스트는
진짜 창을 띄우지 않는다.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
from collections.abc import Callable

from nsm.adapters.notify.windows import encode_command, ps_literal

__all__ = [
    "PickerBusy",
    "PickerNotFolder",
    "PickerTimeout",
    "PickerUnavailable",
    "cancel_picker",
    "pick_folder",
    "picker_script",
    "ps_literal",
]


class PickerUnavailable(Exception):
    """선택 창을 쓸 수 없음(Windows 아님·PowerShell 없음·PowerShell 오류·창을 띄우다 실패)."""


class PickerTimeout(PickerUnavailable):
    """창은 떴지만 제한 시간 안에 고르지 않아 닫았다."""


class PickerBusy(Exception):
    """다른 선택 창이 이미 떠 있음."""


class PickerNotFolder(Exception):
    """OK를 눌렀지만 실제 폴더가 아닌 곳(내 PC·라이브러리·네트워크)이었다."""


# 실행기: (명령 인자, 제한 시간 초) -> 표준 출력(UTF-8로 푼 문자열). 실패는 예외로 알린다
Runner = Callable[[list[str], float], str]

_lock = threading.Lock()  # 선택 창은 한 번에 하나만
_proc: subprocess.Popen | None = None  # 지금 떠 있는 창의 PowerShell(끌 때 닫으려고)
_NOT_FOLDER_EXIT = 3


def picker_script(title: str, initial: str = "") -> str:
    """FolderBrowserDialog를 띄우는 스크립트. OK면 경로 한 줄, 취소면 출력 없음(종료 코드 0).

    창을 띄우다 실패하면(대화형 세션이 아님 등) 오류를 stderr에 쓰고 1, 실제 폴더가 아닌 곳에서 OK면 3으로 끝낸다.
    """
    selected = f"$d.SelectedPath = {ps_literal(initial)}; " if initial else ""
    return (
        "$ErrorActionPreference = 'Stop'; "
        "try { "
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "  # BOM 없이
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::EnableVisualStyles(); "
        # 브라우저 뒤에 숨지 않게: 맨 위에 뜨는(보이지 않는) 폼을 주인으로 넘긴다
        "$o = New-Object System.Windows.Forms.Form; $o.TopMost = $true; $o.ShowInTaskbar = $false; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.Description = {ps_literal(title)}; "
        "$d.ShowNewFolderButton = $true; "
        f"{selected}"
        "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { "
        f"if ($d.SelectedPath) {{ [Console]::Out.WriteLine($d.SelectedPath) }} else {{ exit {_NOT_FOLDER_EXIT} }} "
        "} "
        "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )


def powershell_runner(args: list[str], timeout_s: float) -> str:
    """진짜 실행기. 테스트에서는 쓰지 않는다."""
    global _proc
    if sys.platform != "win32":
        raise PickerUnavailable("Windows가 아님")
    try:
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 콘솔만 숨김, 선택 창은 보임
        )
    except FileNotFoundError as e:
        raise PickerUnavailable("PowerShell 없음") from e
    _proc = p
    try:
        out, err = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        p.kill()  # PowerShell을 끝내면 창도 닫힌다
        p.communicate()
        raise PickerTimeout("시간 초과") from e
    finally:
        _proc = None
    if p.returncode == _NOT_FOLDER_EXIT:
        raise PickerNotFolder
    if p.returncode != 0:
        msg = err.decode("utf-8", "replace").strip()[:300]
        raise PickerUnavailable(f"PowerShell 오류(코드 {p.returncode}): {msg}")
    return out.decode("utf-8-sig", "replace")


def cancel_picker() -> None:
    """떠 있는 선택 창을 닫는다(nsm run을 끌 때). 요청 스레드가 창을 기다리며 종료를 붙잡지 않게."""
    p = _proc
    if p is not None and p.poll() is None:
        with contextlib.suppress(OSError):  # 막 끝난 프로세스
            p.kill()


def pick_folder(
    title: str, initial: str = "", timeout_s: float = 300, runner: Runner | None = None
) -> str | None:
    """선택 창을 띄우고 고른 폴더 경로를 돌려준다. 취소면 None.

    PickerBusy: 다른 창이 떠 있음(기다리지 않고 바로). PickerTimeout: 시간이 지나 닫음.
    PickerNotFolder: 실제 폴더가 아닌 곳을 고름. PickerUnavailable: 창을 쓸 수 없음.
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
        except (PickerUnavailable, PickerNotFolder):
            raise
        except subprocess.TimeoutExpired as e:
            raise PickerTimeout("시간 초과") from e
        except Exception as e:  # noqa: BLE001  실행기의 어떤 실패도 "쓸 수 없음" 하나로
            raise PickerUnavailable(f"{type(e).__name__}: {e}") from e
        # 앞뒤 공백·줄바꿈(\r\n)·BOM을 떼고, 혹시 다른 줄이 섞이면 마지막 줄만
        lines = [s for ln in out.splitlines() if (s := ln.strip().strip("﻿").strip())]
        return lines[-1] if lines else None
    finally:
        _lock.release()
