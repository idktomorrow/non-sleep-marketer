"""폐기한 원고의 카드·미리보기 이미지 정리 (배치 C48, ADR 0073).

- 폐기 즉시: 대시보드 폐기 라우트가 `remove_content_media`를 부른다.
- 새벽 정리(maintenance, 백업 뒤): 폐기 원고 폴더 전부(C48 전에 폐기된 것 포함)와, 게시된 원고 중 카드가 있는
  것의 미리보기(카드와 중복)를 지운다. 게시된 원고의 카드는 화면(검토 목록 썸네일·글 상세)에 쓰여 남긴다.
- 원고 ID(ULID) 모양이 아닌 폴더(`_contract` 같은 실계정 계약 테스트 흔적)와 `style-previews`는 건드리지 않는다.

폴더 위치는 카드 `media/cards/<id>/`(게시 때 렌더), 미리보기 `media/previews/<id>/`(카드 편집),
올린 배경 `media/uploads/backgrounds/<id>/`(카드 편집, `nsm.app.card_editor.upload_dir`).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# 원고 ID는 ULID(26자 Crockford base32). 이 모양이 아니면 경로에 쓰지 않는다(경로 조작 방지)
_CONTENT_ID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")


def is_content_id(cid: str) -> bool:
    return bool(_CONTENT_ID.fullmatch(cid))


def content_media_dirs(media_dir: Path, cid: str) -> tuple[Path, Path, Path]:
    return (
        media_dir / "cards" / cid,
        media_dir / "previews" / cid,
        media_dir / "uploads" / "backgrounds" / cid,
    )


def _remove_tree(d: Path) -> int:
    """폴더를 통째로 지우고 지운 바이트 수를 돌려준다. 없으면 0. 링크는 따라가지 않는다."""
    if d.is_symlink() or not d.is_dir():
        return 0
    size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file() and not p.is_symlink())
    shutil.rmtree(d)
    return size


def remove_content_media(media_dir: Path, cid: str) -> int:
    """원고 하나의 카드·미리보기·올린 배경 폴더를 지운다. 지운 바이트 수(없으면 조용히 0).

    cid가 ULID가 아니면 아무것도 하지 않는다. 지우다 실패하면(권한·파일 잠김) OSError를 그대로 올린다.
    """
    if not is_content_id(cid):
        return 0
    return sum(_remove_tree(d) for d in content_media_dirs(media_dir, cid))


def remove_previews(media_dir: Path, cid: str) -> int:
    """미리보기 폴더만 지운다. 지운 바이트 수."""
    if not is_content_id(cid):
        return 0
    return _remove_tree(media_dir / "previews" / cid)


def _existing(paths: tuple[Path, ...]) -> list[Path]:
    return [p for p in paths if p.is_dir() and not p.is_symlink()]


def sweep_media(media_dir: Path, discarded: list[str], published: list[str]) -> tuple[int, int]:
    """새벽 정리. 폐기 원고는 세 폴더 모두, 게시 원고는 카드(slide-01.png)가 있을 때만 미리보기.

    (지운 폴더 수, 지운 바이트 수). 한 원고에서 실패해도 나머지는 계속하고 WARNING 한 줄씩 남긴다.
    """
    folders = size = 0
    for cid in discarded:
        if not is_content_id(cid):
            continue
        there = _existing(content_media_dirs(media_dir, cid))
        if not there:
            continue
        try:
            size += remove_content_media(media_dir, cid)
        except OSError as e:
            log.warning("폐기 원고 %s 이미지 정리 실패: %s", cid, e)
        folders += sum(1 for d in there if not d.exists())
    for cid in published:
        prev = media_dir / "previews" / cid
        if not is_content_id(cid) or not _existing((prev,)):
            continue
        if not (media_dir / "cards" / cid / "slide-01.png").is_file():
            continue  # 카드가 없으면 미리보기가 화면에 쓰이는 유일한 이미지다
        try:
            size += remove_previews(media_dir, cid)
        except OSError as e:
            log.warning("게시 원고 %s 미리보기 정리 실패: %s", cid, e)
        folders += 0 if prev.exists() else 1
    return folders, size
