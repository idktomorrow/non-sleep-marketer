"""로그온 시 자동 실행 등록.

작업 스케줄러(`schtasks /SC ONLOGON`)는 관리자 권한을 요구하는 경우가 많아서, 권한 없이 되는
사용자 시작프로그램 폴더에 실행 스크립트를 두는 방식을 쓴다 (architecture.md 7절).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SCRIPT_NAME = "non-sleep-marketer.cmd"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA 환경 변수가 없습니다. Windows에서만 지원합니다.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def script_text(project_dir: Path, uv_exe: str, log_file: Path) -> str:
    return (
        "@echo off\r\n"
        "rem non-sleep-marketer autostart (created by nsm install-autostart)\r\n"
        f'cd /d "{project_dir}"\r\n'
        f'start "non-sleep-marketer" /min cmd /c ""{uv_exe}" run nsm run >> "{log_file}" 2>&1"\r\n'
    )


def install(target_dir: Path | None = None, project_dir: Path | None = None) -> str:
    from nsm.config.paths import logs_dir

    target = target_dir or startup_dir()
    project = project_dir or Path(__file__).resolve().parents[2]
    uv = shutil.which("uv") or "uv"
    logs_dir().mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    path = target / SCRIPT_NAME
    # cmd.exe는 시스템 ANSI 코드 페이지(한국어 Windows는 cp949)로 읽는다
    enc = "mbcs" if os.name == "nt" else "utf-8"
    path.write_text(script_text(project, uv, logs_dir() / "nsm-run.log"), encoding=enc)
    return f"로그온 시 자동 실행을 등록했습니다: {path}"


def uninstall(target_dir: Path | None = None) -> str:
    path = (target_dir or startup_dir()) / SCRIPT_NAME
    if path.exists():
        path.unlink()
        return f"자동 실행 등록을 해제했습니다: {path}"
    return "등록된 자동 실행이 없습니다."
