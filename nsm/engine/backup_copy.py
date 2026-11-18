"""백업 파일을 다른 폴더(구글 드라이브·OneDrive 동기화 폴더 등)에 한 부 더 복사 (배치 C49, ADR 0074).

- maintenance가 본 백업을 만든 뒤 `settings.backup_copy_dir`이 있으면 같은 이름으로 복사한다.
  임시 이름으로 복사한 뒤 바꿔 넣어 반쯤 복사된 파일이 남지 않게 한다.
- 복사 폴더도 본 백업과 같은 규칙으로 같은 접두어 파일을 `backup_keep`개만 남긴다(다른 프리셋 파일은 그대로).
- 실패해도 본 백업은 성공이다. 잡은 WARNING과 알림 한 번만 남긴다.
- 설정 화면은 같은 검사(`check_copy_dir`)로 저장 때 안내 문구를 보여 준다(저장은 막지 않음).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
from pathlib import Path

# 검사 결과 코드. 화면 문구는 nsm/app/ko.py BACKUP_COPY_PROBLEMS, 실행 기록 문구는 아래 REASONS
RELATIVE = "relative"
MISSING = "missing"
NOT_DIR = "not_dir"
NOT_WRITABLE = "not_writable"
INSIDE_BACKUPS = "inside_backups"

REASONS = {
    RELATIVE: "절대 경로가 아님",
    MISSING: "폴더가 없음",
    NOT_DIR: "폴더가 아님",
    NOT_WRITABLE: "쓸 수 없는 폴더",
    INSIDE_BACKUPS: "백업 폴더 안이라 복사하지 않음",
}


class CopyError(Exception):
    """복사하지 못한 이유(실행 기록·알림에 그대로 쓴다)."""


def clean_path(raw: str) -> str:
    """탐색기 "경로로 복사"가 붙이는 앞뒤 따옴표와 공백을 뗀다."""
    return raw.strip().strip('"').strip()


def _inside(path: Path, root: Path) -> bool:
    try:
        p, r = path.resolve(), root.resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def check_copy_dir(raw: str, backups_dir: Path | None, probe: bool = True) -> str | None:
    """문제가 없으면 None, 있으면 코드. probe면 임시 파일을 하나 만들었다 지워 쓰기를 확인한다."""
    path = Path(clean_path(raw))
    if not path.is_absolute():
        return RELATIVE
    if backups_dir is not None and _inside(path, backups_dir):
        return INSIDE_BACKUPS
    if not path.exists():
        return MISSING
    if not path.is_dir():
        return NOT_DIR
    if probe:
        test = path / f".nsm-write-test-{secrets.token_hex(4)}"
        try:
            test.write_bytes(b"")
            test.unlink()
        except OSError:
            return NOT_WRITABLE
    return None


def prune_copies(folder: Path, prefix: str, keep: int, newest: Path) -> int:
    """같은 접두어 백업을 keep개만 남긴다. 방금 만든 것(newest)은 지우지 않는다. 지운 개수."""
    others = [p for p in folder.glob(f"{prefix}-????-??-??.duckdb") if p != newest]
    removed = 0
    for old in sorted(others, reverse=True)[keep - 1 :]:
        old.unlink(missing_ok=True)
        removed += 1
    return removed


def copy_backup(src: Path, raw_dir: str, backups_dir: Path) -> Path:
    """본 백업 src를 복사 폴더에 같은 이름으로 복사한다. 복사한 파일 경로.

    복사하지 못하면 CopyError(이유). 쓰기 확인은 실제 복사가 하므로 임시 파일을 따로 만들지 않는다.
    """
    problem = check_copy_dir(raw_dir, backups_dir, probe=False)
    if problem:
        raise CopyError(REASONS[problem])
    folder = Path(clean_path(raw_dir))
    final = folder / src.name
    tmp = folder / f".{src.name}.{secrets.token_hex(4)}.tmp"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, final)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise CopyError(f"복사 실패({e.strerror or type(e).__name__})") from e
    return final
