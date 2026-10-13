"""Windows 데스크톱 알림. 실패하거나 Windows가 아니면 로그로만 남긴다.

Windows 11은 등록되지 않은 앱 ID로 보낸 알림을 오류 없이 버린다. 그래서 처음 보낼 때 앱 ID를
HKCU\\Software\\Classes\\AppUserModelId에 표시 이름과 함께 등록한다(관리자 권한 불필요, ADR 0030).
알림 XML은 파이썬에서 이스케이프해 PowerShell 작은따옴표 문자열로 넣고 -EncodedCommand로 넘긴다.
알림 문구(원고 주제·오류 메시지)가 PowerShell 코드로 해석될 틈을 두지 않는다.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from xml.sax.saxutils import escape, quoteattr

log = logging.getLogger("nsm.notify")

APP_ID = "NonSleepMarketer"
DISPLAY_NAME = "논슬립 마케터"
_AUMID_KEY = r"Software\Classes\AppUserModelId\NonSleepMarketer"


def register_app_id() -> None:
    """알림 발신자를 Windows에 등록한다(이미 있으면 덮어씀, 사용자 레지스트리만)."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)


def toast_script(title: str, body: str, url: str = "") -> str:
    """알림 하나를 띄우는 PowerShell 스크립트. 사용자 문구는 XML 이스케이프 후 작은따옴표 리터럴 안에만 들어간다."""
    launch = f" activationType={quoteattr('protocol')} launch={quoteattr(url)}" if url else ""
    actions = (
        f"<actions><action content={quoteattr('대시보드 열기')} activationType={quoteattr('protocol')}"
        f" arguments={quoteattr(url)}/></actions>"
        if url
        else ""
    )
    xml = (
        f"<toast{launch}><visual><binding template={quoteattr('ToastGeneric')}>"
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        f"</binding></visual>{actions}</toast>"
    )
    lit = xml.replace("'", "''")  # PowerShell 작은따옴표 문자열: 변수·$()가 해석되지 않는다
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null; "
        f"$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml('{lit}'); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )


def encode_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class DesktopNotifier:
    def __init__(
        self,
        dashboard_url: str = "",
        min_interval_s: float = 60.0,
        enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.dashboard_url = dashboard_url
        self.enabled = enabled  # 설정 화면의 알림 켜기/끄기(실행 중에 바뀌므로 매번 읽는다)
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}
        self._registered = False

    def notify(self, title: str, body: str, level: str) -> None:
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(
            "%s: %s", title, body
        )
        # 같은 제목 알림이 짧은 시간에 쏟아지지 않게
        now = time.monotonic()
        if now - self._last.get(title, -1e9) < self.min_interval_s:
            return
        self._last[title] = now
        if sys.platform != "win32" or not self.enabled():
            return
        # 작업 스레드를 붙잡지 않게 따로 보낸다(PowerShell 시작에 1~2초)
        threading.Thread(target=self._send, args=(title, body[:250]), daemon=True).start()

    def _send(self, title: str, body: str) -> None:
        try:
            if not self._registered:
                register_app_id()
                self._registered = True
            r = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encode_command(toast_script(title, body, self.dashboard_url)),
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", "replace").strip()[:300]
                log.warning("데스크톱 알림 실패(코드 %s): %s", r.returncode, err)
        except Exception as e:  # noqa: BLE001  알림 실패가 잡을 멈추면 안 된다
            log.warning("데스크톱 알림 실패: %s", e)
