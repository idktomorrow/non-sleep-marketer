from __future__ import annotations

from datetime import UTC, datetime

from ulid import ULID


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def new_id() -> str:
    """정렬 가능한 고유 ID (ULID 문자열)."""
    return str(ULID())
