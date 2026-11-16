"""폐기한 원고의 카드·미리보기 이미지 정리 (배치 C48). 파일은 전부 tmp_path 아래."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from nsm.domain.clock import new_id
from nsm.domain.models import Content, ContentBrief
from nsm.engine import media_cleanup as M
from nsm.engine.context import JobContext
from nsm.engine.maintenance import run_maintenance
from nsm.engine.summary import describe
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FakeTextGenerator, load_source_items


def _ctx(db, clock, app_config, media: Path) -> JobContext:
    return JobContext(
        config=app_config,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        media_dir=media,
    )


def _add(repo: Repository, app_config, status: str) -> str:
    cid = new_id()
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립", template="info", sources=load_source_items()[:2]),
        app_config.persona,
        app_config.brand,
    )
    repo.insert_content(
        Content(
            id=cid,
            status=status,
            generated=gen,
            relevance_confidence=0.9,
            created_at=repo.clock.now(),
        )
    )
    return cid


def _png(d: Path, name: str = "slide-01.png", size: int = 1000) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x" * size)


def _all_three(media: Path, cid: str) -> tuple[Path, Path, Path]:
    cards, prev, up = M.content_media_dirs(media, cid)
    _png(cards)
    _png(cards, "slide-02.png")
    (cards / "overflow.txt").write_text("", "utf-8")
    _png(prev)
    _png(up, "slide-01-abcd.jpg", 500)
    return cards, prev, up


# ------------------------------------------------------------------ remove_content_media
def test_remove_content_media_removes_three_folders_and_returns_bytes(tmp_path) -> None:
    media = tmp_path / "media"
    cid, other = new_id(), new_id()
    dirs = _all_three(media, cid)
    keep = _all_three(media, other)
    assert M.remove_content_media(media, cid) == 1000 * 3 + 500
    assert not any(d.exists() for d in dirs)
    assert all(d.is_dir() for d in keep)  # 다른 원고 폴더는 그대로
    assert M.remove_content_media(media, cid) == 0  # 다시 불러도 조용히 0


def test_missing_media_folder_is_zero(tmp_path) -> None:
    assert M.remove_content_media(tmp_path / "nope", new_id()) == 0
    assert M.sweep_media(tmp_path / "nope", [new_id()], [new_id()]) == (0, 0)


@pytest.mark.parametrize("cid", ["_contract", "..", "../x", "c1", "", "01ARZ3NDEKTSV4RRFFQ69G5FA"])
def test_non_ulid_id_removes_nothing(tmp_path, cid: str) -> None:
    media = tmp_path / "media"
    _png(media / "cards" / "_contract")
    _png(media / "previews" / "c1")
    _png(media / "cards")  # media/cards/slide-01.png: ".."·""이 가리킬 수 있는 곳
    assert M.remove_content_media(media / "cards" / "x", cid) == 0
    assert M.remove_content_media(media, cid) == 0
    assert M.sweep_media(media, [cid], [cid]) == (0, 0)
    assert (media / "cards" / "_contract" / "slide-01.png").is_file()
    assert (media / "previews" / "c1" / "slide-01.png").is_file()
    assert (media / "cards" / "slide-01.png").is_file()


def test_lowercase_or_bad_letter_is_not_a_content_id() -> None:
    cid = new_id()
    assert M.is_content_id(cid)
    assert not M.is_content_id(cid.lower())
    assert not M.is_content_id(cid[:-1] + "U")  # Crockford에 없는 글자
    assert not M.is_content_id("8" + cid[1:])  # 첫 글자는 0-7


# ------------------------------------------------------------------ maintenance
def test_maintenance_sweeps_discarded_and_duplicate_previews_only(db, clock, app_config, tmp_path):
    media = tmp_path / "media"
    ctx = _ctx(db, clock, app_config, media)
    gone = _add(ctx.repo, app_config, "discarded")
    pub = _add(ctx.repo, app_config, "published")
    pub_no_card = _add(ctx.repo, app_config, "published")
    kept = {s: _add(ctx.repo, app_config, s) for s in ("approved", "pending_review", "draft")}
    gone_dirs = _all_three(media, gone)
    _png(media / "cards" / pub)
    _png(media / "previews" / pub, size=2000)
    _png(media / "previews" / pub_no_card)
    for cid in kept.values():
        _all_three(media, cid)
    _png(media / "cards" / "_contract")
    _png(media / "style-previews" / "abc123")

    r = run_maintenance(ctx)

    assert r.ok
    assert not any(d.exists() for d in gone_dirs)
    assert not (media / "previews" / pub).exists()
    assert (media / "cards" / pub / "slide-01.png").is_file()  # 게시 카드는 화면에 쓰여 영구
    # 카드가 없으면 미리보기 유지
    assert (media / "previews" / pub_no_card / "slide-01.png").is_file()
    for cid in kept.values():
        assert all(d.is_dir() for d in M.content_media_dirs(media, cid))
    assert (media / "cards" / "_contract" / "slide-01.png").is_file()
    assert (media / "style-previews" / "abc123" / "slide-01.png").is_file()
    assert r.counts["media_removed"] == 4  # 폐기 3 + 중복 미리보기 1
    assert r.counts["media_mb"] == round((3500 + 2000) / 1_000_000, 1)
    # 두 번째는 정리할 것 없음
    again = run_maintenance(ctx)
    assert again.counts["media_removed"] == 0


def test_maintenance_without_media_folder_is_zero(db, clock, app_config, tmp_path) -> None:
    ctx = _ctx(db, clock, app_config, tmp_path / "media")
    _add(ctx.repo, app_config, "discarded")
    r = run_maintenance(ctx)
    assert r.ok and r.counts["media_removed"] == 0 and r.counts["media_mb"] == 0
    assert "이미지" not in describe("maintenance", counts=r.counts)


def test_maintenance_summary_mentions_media_only_when_removed() -> None:
    assert "이미지 3개 폴더 정리(2.5MB)" in describe(
        "maintenance", counts={"media_removed": 3, "media_mb": 2.5}
    )
    assert "이미지" not in describe("maintenance", counts={"media_removed": 0, "media_mb": 0})


def test_delete_failure_does_not_fail_maintenance(
    db, clock, app_config, tmp_path, monkeypatch, caplog
) -> None:
    media = tmp_path / "media"
    ctx = _ctx(db, clock, app_config, media)
    stuck, ok = (_add(ctx.repo, app_config, "discarded") for _ in range(2))
    _all_three(media, stuck)
    ok_dirs = _all_three(media, ok)
    real = M.shutil.rmtree

    def rmtree(path, *a, **kw):
        if stuck in str(path):
            raise PermissionError("파일이 열려 있음")
        return real(path, *a, **kw)

    monkeypatch.setattr(M.shutil, "rmtree", rmtree)
    with caplog.at_level(logging.WARNING):
        r = run_maintenance(ctx)
    assert r.ok and not r.errors
    assert not any(d.exists() for d in ok_dirs)  # 실패한 원고 다음 것도 정리
    assert r.counts["media_removed"] == 3
    assert all(d.is_dir() for d in M.content_media_dirs(media, stuck))
    assert any("이미지 정리 실패" in m for m in caplog.messages)


def test_unexpected_error_in_sweep_keeps_other_cleanup(
    db, clock, app_config, tmp_path, monkeypatch
) -> None:
    ctx = _ctx(db, clock, app_config, tmp_path / "media")

    def boom(*a, **kw):
        raise RuntimeError("뜻밖의 오류")

    monkeypatch.setattr(M, "sweep_media", boom)
    r = run_maintenance(ctx)
    assert r.ok and "media_removed" not in r.counts and "runs_pruned" in r.counts


def test_partial_failure_still_removes_and_counts_other_folders(tmp_path, monkeypatch) -> None:
    media = tmp_path / "media"
    cid = new_id()
    cards, prev, up = _all_three(media, cid)
    real = M.shutil.rmtree

    def rmtree(path, *a, **kw):
        if Path(path) == prev:
            raise PermissionError("브라우저가 미리보기를 열고 있음")
        return real(path, *a, **kw)

    monkeypatch.setattr(M.shutil, "rmtree", rmtree)
    with pytest.raises(PermissionError):
        M.remove_content_media(media, cid)  # 라우트는 이 오류를 WARNING으로 받는다
    assert not cards.exists() and not up.exists() and prev.is_dir()  # 막힌 것 뒤의 폴더도 지움
    _all_three(media, cid)
    assert M.sweep_media(media, [cid], []) == (2, 2000 + 500)  # 지운 폴더의 크기는 센다
