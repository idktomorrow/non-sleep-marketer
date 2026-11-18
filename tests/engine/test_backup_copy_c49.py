"""백업 파일을 다른 폴더에 한 부 더 복사 (배치 C49). 파일은 전부 tmp_path 아래."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from nsm.engine import backup_copy as B
from nsm.engine.context import JobContext
from nsm.engine.maintenance import run_maintenance
from nsm.engine.summary import describe
from nsm.store.db import Database
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier


@pytest.fixture
def file_db(tmp_path: Path):
    d = Database(tmp_path / "data.duckdb")
    yield d
    d.close()


def _ctx(file_db, clock, app_config, tmp_path: Path, copy_dir: str = "", **settings) -> JobContext:
    cfg = app_config.model_copy(
        update={
            "settings": app_config.settings.model_copy(
                update={"backup_copy_dir": copy_dir, **settings}
            )
        }
    )
    return JobContext(
        config=cfg,
        repo=Repository(file_db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        backups_dir=tmp_path / "backups",
    )


def test_no_copy_when_setting_is_empty(file_db, clock, app_config, tmp_path) -> None:
    r = run_maintenance(_ctx(file_db, clock, app_config, tmp_path))
    assert r.ok and r.counts["backup"]
    assert not {"copy_ok", "copy_error", "copies_removed", "copy_dir"} & set(r.counts)
    assert "복사" not in describe("maintenance", counts=r.counts)


def test_copy_has_same_name_and_content(file_db, clock, app_config, tmp_path) -> None:
    dest = tmp_path / "OneDrive" / "nsm"
    dest.mkdir(parents=True)
    ctx = _ctx(file_db, clock, app_config, tmp_path, f'"{dest}" ')  # 탐색기 "경로로 복사" 따옴표
    r = run_maintenance(ctx)
    src = tmp_path / "backups" / r.counts["backup"]
    copied = dest / src.name
    assert r.ok and r.counts["copy_ok"] is True and r.counts["copies_removed"] == 0
    assert copied.read_bytes() == src.read_bytes()
    assert not list(dest.glob(".*.tmp"))  # 임시 파일이 남지 않음
    assert f"복사: {dest}" in describe("maintenance", counts=r.counts)
    assert ctx.notifier.calls == []


def test_copy_folder_keeps_backup_keep_and_ignores_other_presets(
    file_db, clock, app_config, tmp_path
) -> None:
    dest = tmp_path / "copy"
    dest.mkdir()
    today = clock.now().date()
    old = [dest / f"data-{(today - timedelta(days=d)).isoformat()}.duckdb" for d in range(1, 6)]
    for p in old:
        p.write_bytes(b"old")
    other = dest / f"data-work-{(today - timedelta(days=9)).isoformat()}.duckdb"
    other.write_bytes(b"other preset")
    stray = dest / "my-notes.txt"
    stray.write_text("keep", "utf-8")
    ctx = _ctx(file_db, clock, app_config, tmp_path, str(dest), backup_keep=3)
    r = run_maintenance(ctx)
    left = sorted(p.name for p in dest.glob("data-????-??-??.duckdb"))
    assert r.counts["backup"] in left  # 방금 복사한 것은 남음
    assert len(left) == 3 and r.counts["copies_removed"] == 3
    assert left[:2] == [old[1].name, old[0].name]  # 가장 최근 옛 파일 둘
    assert other.read_bytes() == b"other preset" and stray.is_file()


def test_missing_folder_keeps_backup_and_notifies_once(file_db, clock, app_config, tmp_path):
    ctx = _ctx(file_db, clock, app_config, tmp_path, str(tmp_path / "nope"))
    r = run_maintenance(ctx)
    assert r.ok and not r.errors
    assert (tmp_path / "backups" / r.counts["backup"]).is_file()
    assert r.counts["copy_ok"] is False and r.counts["copy_error"] == "폴더가 없음"
    assert [c[0] for c in ctx.notifier.calls] == ["백업 복사 실패"]
    assert "복사 실패: 폴더가 없음" in describe("maintenance", counts=r.counts)


def test_write_failure_keeps_backup_and_leaves_no_partial_file(
    file_db, clock, app_config, tmp_path, monkeypatch, caplog
) -> None:
    dest = tmp_path / "copy"
    dest.mkdir()

    def half_copy(src, dst, *a, **kw):
        Path(dst).write_bytes(b"half")
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(B.shutil, "copy2", half_copy)
    ctx = _ctx(file_db, clock, app_config, tmp_path, str(dest))
    with caplog.at_level(logging.WARNING):
        r = run_maintenance(ctx)
    assert r.ok and not r.errors and r.counts["backup"]
    assert r.counts["copy_ok"] is False and "Permission denied" in r.counts["copy_error"]
    assert list(dest.iterdir()) == []  # 반쯤 복사된 임시 파일도 지움
    assert len(ctx.notifier.calls) == 1
    assert any("백업 복사 실패" in m for m in caplog.messages)


@pytest.mark.parametrize("sub", ["", "inner"])
def test_backups_folder_itself_or_inside_is_not_copied(
    file_db, clock, app_config, tmp_path, sub: str
) -> None:
    target = tmp_path / "backups" / sub if sub else tmp_path / "backups"
    target.mkdir(parents=True, exist_ok=True)
    ctx = _ctx(file_db, clock, app_config, tmp_path, str(target))
    r = run_maintenance(ctx)
    assert r.ok and r.counts["copy_ok"] is False
    assert r.counts["copy_error"] == B.REASONS[B.INSIDE_BACKUPS]
    assert len(list((tmp_path / "backups").rglob("data-*.duckdb"))) == 1  # 두 번 두지 않음


def test_check_copy_dir_codes(tmp_path, monkeypatch) -> None:
    backups = tmp_path / "backups"
    ok = tmp_path / "ok"
    ok.mkdir()
    (tmp_path / "file.txt").write_text("x", "utf-8")
    assert B.check_copy_dir(str(ok), backups) is None
    assert not list(ok.iterdir())  # 확인용 임시 파일은 지움
    assert B.check_copy_dir("relative\\dir", backups) == B.RELATIVE
    assert B.check_copy_dir(str(tmp_path / "nope"), backups) == B.MISSING
    assert B.check_copy_dir(str(tmp_path / "file.txt"), backups) == B.NOT_DIR
    assert B.check_copy_dir(str(backups / "x"), backups) == B.INSIDE_BACKUPS
    real = Path.write_bytes

    def denied(self, data):
        if self.name.startswith(".nsm-write-test"):
            raise PermissionError(13, "Permission denied")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", denied)
    assert B.check_copy_dir(str(ok), backups) == B.NOT_WRITABLE
    assert B.check_copy_dir(str(ok), backups, probe=False) is None


def test_run_now_uses_the_same_maintenance_job() -> None:
    """대시보드 "지금 실행 → 백업·정리"는 같은 maintenance 잡을 돌린다(따로 고칠 곳 없음)."""
    from nsm.app.ko import JOBS as LABELS
    from nsm.engine.runner import JOBS

    assert JOBS["maintenance"] is run_maintenance
    assert "maintenance" in LABELS
