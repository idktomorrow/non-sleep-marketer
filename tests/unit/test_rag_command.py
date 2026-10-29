"""R1: `nsm index` 몸통(run_index). 실제 키·네트워크 없이 가짜 임베더로."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nsm.rag.command import DbLockedError, is_lock_error, open_db, run_index
from tests.fakes import FakeEmbedder


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    (root / "adr" / "0001-a.md").write_text("# 0001. 가\n\n## 결정\n\n본문입니다.\n", "utf-8")
    (root / "glossary.md").write_text("# 용어집\n\n| 용어 | 뜻 |\n|---|---|\n| a | b |\n", "utf-8")
    return root


def _run(app_config, db: Path, docs: Path, out: list[str], **kw) -> int:
    return run_index(app_config, db, docs=docs, echo=out.append, **kw)


def test_dry_run_prints_stats_without_db_or_api(app_config, docs: Path, tmp_path: Path) -> None:
    out: list[str] = []
    db = tmp_path / "never.duckdb"
    assert _run(app_config, db, docs, out, dry_run=True) == 0
    assert not db.exists()  # DB를 만들지도 않는다
    assert "조각 2개" in out[1] and "파일 2개" in out[1]


def test_index_then_stats(app_config, docs: Path, tmp_path: Path) -> None:
    out: list[str] = []
    db = tmp_path / "rag.duckdb"
    emb = FakeEmbedder()
    code = _run(app_config, db, docs, out, secrets=object(), embedder_factory=lambda c, s, r: emb)
    assert code == 0 and "새로 임베딩 2개" in out[1]
    out.clear()
    assert _run(app_config, db, docs, out, stats=True) == 0
    assert "조각 2개 · 파일 2개 · 벡터 없는 조각 0개" in out[1]
    assert "임베딩 모델 fake-embed: 2개" in out[2]


def test_no_key_message(app_config, docs: Path, tmp_path: Path) -> None:
    out: list[str] = []
    code = _run(
        app_config,
        tmp_path / "r.duckdb",
        docs,
        out,
        secrets=object(),
        embedder_factory=lambda *a: None,
    )
    assert code == 0
    assert "벡터 없음 2개" in out[1]
    assert any("Gemini 키가 없어 조각만 저장" in line for line in out)


def test_lock_error_detection_messages() -> None:
    win = 'IO Error: Cannot open file "C:\\x\\data.duckdb": 다른 프로세스가 사용 중\n\nFile is already open in \nC:\\py\\python.exe (PID 1234)'
    posix = 'IO Error: Could not set lock on file "/x/data.duckdb": Conflicting lock is held in python (PID 1)'
    assert is_lock_error(Exception(win)) and is_lock_error(Exception(posix))
    assert not is_lock_error(Exception("Catalog Error: table not found"))


def test_real_lock_from_another_process_gives_korean_help(tmp_path: Path) -> None:
    """운영 nsm run이 DB를 쥔 상황을 다른 프로세스로 재현한다."""
    db = tmp_path / "held.duckdb"
    code = (
        f"import duckdb,time; c=duckdb.connect(r'{db}'); print('held', flush=True); time.sleep(20)"
    )
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(DbLockedError) as exc:
            open_db(db)
        assert "nsm run" in str(exc.value) and "--db" in str(exc.value)
    finally:
        holder.kill()
        holder.wait()
