"""DuckDB 연결과 스키마 적용.

DuckDB는 한 프로세스만 파일을 읽기/쓰기로 열 수 있다(ADR 0006). 그래서 `nsm run` 프로세스가
Database 하나를 만들고, 스케줄러·대시보드 스레드는 `cursor()`로 각자 커서를 받아 쓴다.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import duckdb

SCHEMA_VERSION = 3

# 버전 n-1 -> n 으로 올리는 SQL. schema.sql은 항상 최신 형태(새 설치용)이고,
# 기존 사용자 DB는 여기 단계를 차례로 적용해 따라잡는다.
# 예: MIGRATIONS = {2: ["ALTER TABLE contents ADD COLUMN foo VARCHAR"]}
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE contents ADD COLUMN persona_topic VARCHAR"],
    3: [],  # notifications 테이블: schema.sql의 CREATE TABLE IF NOT EXISTS가 만든다(N2)
}


def to_db(dt: datetime | None) -> datetime | None:
    """aware datetime → UTC naive (DB 저장용)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime은 저장할 수 없습니다. timezone을 붙이세요.")
    return dt.astimezone(UTC).replace(tzinfo=None)


def from_db(dt: datetime | None) -> datetime | None:
    """UTC naive (DB) → aware UTC datetime."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC)


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(self.path)
        self._lock = threading.RLock()
        self._apply_schema()

    def _apply_schema(self) -> None:
        sql = resources.files("nsm.store").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_version'"
            ).fetchone()
            fresh = not row or row[0] == 0
            self._conn.execute(sql)
            if fresh:
                self._conn.execute("INSERT INTO schema_version VALUES (?)", [SCHEMA_VERSION])
                return
            current = self._read_version(self._conn)
            for v in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS.get(v, []):
                    self._conn.execute(stmt)
                self._conn.execute("INSERT INTO schema_version VALUES (?)", [v])

    @staticmethod
    def _read_version(conn: duckdb.DuckDBPyConnection) -> int:
        row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    @property
    def schema_version(self) -> int:
        cur = self._conn.cursor()
        try:
            return self._read_version(cur)
        finally:
            cur.close()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """스레드마다 별도 커서를 쓴다."""
        return self._conn.cursor()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """여러 쓰기를 원자적으로 묶는다."""
        cur = self._conn.cursor()
        with self._lock:
            cur.execute("BEGIN TRANSACTION")
            try:
                yield cur
            except Exception:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")
            finally:
                cur.close()

    def execute(self, sql: str, params: list | tuple | None = None) -> list[tuple]:
        """단일 문장 실행. lock으로 직렬화해 스레드 간 트랜잭션 충돌을 피한다."""
        cur = self._conn.cursor()
        try:
            with self._lock:
                cur.execute(sql, params or [])
            try:
                return cur.fetchall()
            except duckdb.InvalidInputException:
                return []
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()
