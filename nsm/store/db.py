"""DuckDB 연결과 스키마 적용.

DuckDB는 한 프로세스만 파일을 읽기/쓰기로 열 수 있다(ADR 0006). 그래서 `nsm run` 프로세스가
Database 하나를 만들고, 스케줄러·대시보드 스레드는 `cursor()`로 각자 커서를 받아 쓴다.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import duckdb

SCHEMA_VERSION = 4

# 버전 n-1 -> n 으로 올리는 SQL. schema.sql은 항상 최신 형태(새 설치용)이고,
# 기존 사용자 DB는 여기 단계를 차례로 적용해 따라잡는다.
# 예: MIGRATIONS = {2: ["ALTER TABLE contents ADD COLUMN foo VARCHAR"]}
MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE contents ADD COLUMN persona_topic VARCHAR"],
    3: [],  # notifications 테이블: schema.sql의 CREATE TABLE IF NOT EXISTS가 만든다(N2)
    4: ["ALTER TABLE source_items ADD COLUMN IF NOT EXISTS facts VARCHAR"],  # C31: 구체 사실 유무
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

    def _apply_schema(self, conn: duckdb.DuckDBPyConnection | None = None) -> None:
        sql = resources.files("nsm.store").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._lock:
            conn = conn or self._conn
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = 'schema_version'"
            ).fetchone()
            fresh = not row or row[0] == 0
            conn.execute(sql)
            if fresh:
                conn.execute("INSERT INTO schema_version VALUES (?)", [SCHEMA_VERSION])
                return
            current = self._read_version(conn)
            for v in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in MIGRATIONS.get(v, []):
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_version VALUES (?)", [v])

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
        """스레드마다 별도 커서를 쓴다. 커서는 lock 안에서 만든다(switch 도중에 옛 연결을 잡지 않게)."""
        with self._lock:
            return self._conn.cursor()

    def switch(self, path: str | Path) -> None:
        """다른 DB 파일로 갈아끼운다(프리셋 전환, C29). 이 객체를 쥔 Repository들은 그대로 쓴다.

        새 파일을 먼저 열어 스키마까지 맞춘 뒤에야 바꾸고 옛 연결을 닫는다. 새 파일이 깨졌으면 예외를 내고
        옛 연결은 그대로 남는다. lock 안에서 하므로 작업 스레드가 쉬는 틈에 불러야 한다.
        """
        new_path = str(path)
        if new_path != ":memory:":
            Path(new_path).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if new_path == self.path:
                return
            fresh = duckdb.connect(new_path)
            try:
                self._apply_schema(fresh)
            except Exception:
                fresh.close()
                raise
            old, self._conn, self.path = self._conn, fresh, new_path
            old.close()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """여러 쓰기를 원자적으로 묶는다."""
        with self._lock:
            cur = self._conn.cursor()
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
        """단일 문장 실행. lock으로 직렬화해 스레드 간 트랜잭션 충돌을 피하고, switch() 중에 옛 연결의 커서를
        쓰지 않게 커서 생성부터 fetch까지 lock 안에서 끝낸다."""
        rows, _ = self.query(sql, params)
        return rows

    def query(self, sql: str, params: list | tuple | None = None) -> tuple[list[tuple], list[str]]:
        """execute + 열 이름. 대시보드 조회(queries.py)가 쓴다."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(sql, params or [])
                cols = [d[0] for d in cur.description] if cur.description else []
                try:
                    return cur.fetchall(), cols
                except duckdb.InvalidInputException:
                    return [], cols
            finally:
                cur.close()

    def backup_to(self, target: Path) -> Path:
        """지금 DB를 target 파일로 통째로 복사한다(C12 자동 백업).

        DuckDB는 파일을 연 프로세스만 읽을 수 있어 파일 복사 대신 `COPY FROM DATABASE`를 쓴다.
        임시 파일에 다 쓴 뒤 이름을 바꿔, 도중에 꺼져도 반쯤 쓴 백업이 남지 않게 한다.
        """
        if self.path == ":memory:":
            raise ValueError("메모리 DB는 백업하지 않습니다")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        for leftover in (tmp, tmp.with_name(tmp.name + ".wal")):
            leftover.unlink(missing_ok=True)
        path = str(tmp).replace("'", "''")
        try:
            with self._lock:
                name = self._conn.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
                self._conn.execute(f"ATTACH '{path}' AS nsm_backup")
                try:
                    self._conn.execute(f'COPY FROM DATABASE "{name}" TO nsm_backup')
                finally:
                    self._conn.execute("DETACH nsm_backup")
            os.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)  # 반쯤 쓴 임시 파일을 남기지 않는다
            raise
        return target

    def close(self) -> None:
        self._conn.close()
