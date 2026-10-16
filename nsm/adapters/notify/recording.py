"""알림을 대시보드 알림함(종 모양)에 남기고 데스크톱 알림으로도 보낸다(N2, ADR 0031).

Notifier 포트(title, body, level)는 그대로 두고, 알림 제목으로 "누르면 갈 화면"을 정한다.
"""

from __future__ import annotations

import logging

from nsm.ports import Notifier
from nsm.store.repo import Repository

log = logging.getLogger("nsm.notify")

# 제목 앞부분 → 대시보드 경로. 먼저 맞는 것을 쓴다.
_LINKS: tuple[tuple[str, str], ...] = (
    ("검토 대기", "/review"),
    ("카드 글 넘침", "/review"),
    ("댓글", "/comments"),
    ("게시 포기", "/queue"),
    ("게시 실패", "/queue"),
    ("게시하기", "/queue"),
    ("로그인", "/"),
    ("계정", "/"),
    ("비용 상한", "/performance"),
    ("주간 리포트", "/reports"),
    ("원고 만들기", "/review"),
    ("자료 모으기", "/"),
    ("계속 0건", "/"),
    ("논슬립 마케터 알림 테스트", "/settings"),
)


def link_for(title: str) -> str:
    for prefix, link in _LINKS:
        if title.startswith(prefix):
            return link
    return "/logs"


class RecordingNotifier:
    def __init__(self, repo: Repository, desktop: Notifier | None = None) -> None:
        self.repo = repo
        self.desktop = desktop

    def notify(self, title: str, body: str, level: str) -> None:
        try:
            self.repo.add_notification(title, body, level, link_for(title))
            self.repo.prune_notifications()
        except Exception as e:  # noqa: BLE001  알림함 기록 실패가 잡을 멈추면 안 된다
            log.warning("알림함 기록 실패: %s", e)
        if self.desktop is not None:
            self.desktop.notify(title, body, level)  # type: ignore[arg-type]
