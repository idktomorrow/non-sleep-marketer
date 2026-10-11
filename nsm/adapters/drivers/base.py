"""브라우저 드라이버: 플랫폼 어댑터·수집 소스가 쓰는 얇은 브라우저 조작 층 (ADR 0002).

포트가 아니라 어댑터 내부 세부사항이다. 수집에 필요한 최소 동작만 정의하고,
게시에 필요한 동작(click, fill, upload)은 배치 G에서 추가한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    context: str  # 링크를 감싼 블록의 텍스트(요약문 추출용, 최대 400자)


class BrowserDriver(Protocol):
    def goto(self, url: str) -> None: ...
    def links(self) -> list[Link]: ...
    def page_text(self) -> str: ...
    def close(self) -> None: ...


class DriverUnavailableError(RuntimeError):
    pass
