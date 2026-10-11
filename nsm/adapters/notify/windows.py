"""Windows 데스크톱 알림(winotify). 실패하거나 Windows가 아니면 로그로만 남긴다."""

from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("nsm.notify")

APP_ID = "논슬립 마케터"


class DesktopNotifier:
    def __init__(self, dashboard_url: str = "", min_interval_s: float = 60.0) -> None:
        self.dashboard_url = dashboard_url
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}

    def notify(self, title: str, body: str, level: str) -> None:
        getattr(log, "error" if level == "error" else "warning" if level == "warn" else "info")(
            "%s: %s", title, body
        )
        # 같은 제목 알림이 짧은 시간에 쏟아지지 않게
        now = time.monotonic()
        if now - self._last.get(title, -1e9) < self.min_interval_s:
            return
        self._last[title] = now
        if sys.platform != "win32":
            return
        try:
            from winotify import Notification

            n = Notification(app_id=APP_ID, title=title, msg=body[:250], duration="short")
            if self.dashboard_url:
                n.add_actions(label="대시보드 열기", launch=self.dashboard_url)
            n.show()
        except Exception as e:  # noqa: BLE001  알림 실패가 잡을 멈추면 안 된다
            log.warning("데스크톱 알림 실패: %s", e)
