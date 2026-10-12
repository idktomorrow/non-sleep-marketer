"""드라이런 어댑터: 미리보기 파일과 가짜 댓글·지표 (경계 2의 드라이런 부분, 기본 실행 포함)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nsm.adapters.platforms import PLATFORM_CAPABILITIES
from nsm.adapters.platforms.dryrun import DryRunAdapter
from nsm.domain.models import RenderedPost
from nsm.ports import CAPABILITIES, NotSupportedError, PlatformAdapter


def _post(tmp_path: Path, platform="threads") -> RenderedPost:
    img = tmp_path / "slide-01.png"
    img.write_bytes(b"\x89PNG")
    return RenderedPost(
        content_id="c1",
        platform=platform,
        text="AI를 기반으로 생성된 가상인물이 포함된 게시물입니다\n\n본문",
        images=[img],
        title="제목 [가상인물 포함]" if platform == "naver_blog" else None,
    )


def test_dryrun_is_a_platform_adapter(tmp_path: Path, clock) -> None:
    a = DryRunAdapter("threads", tmp_path / "preview", clock)
    assert isinstance(a, PlatformAdapter)
    assert a.capabilities() == set(CAPABILITIES)  # 스레드는 숨기기까지 전부 지원
    assert a.health().logged_in


def test_publish_writes_preview_files(tmp_path: Path, clock) -> None:
    preview = tmp_path / "preview"
    a = DryRunAdapter("naver_blog", preview, clock)
    res = a.publish(_post(tmp_path, "naver_blog"))
    out = preview / "2026-10-11" / "c1" / "naver_blog"
    assert (out / "post.md").read_text("utf-8").startswith("# 제목 [가상인물 포함]")
    assert (out / "slide-01.png").exists()
    assert res.external_id == "dryrun-naver_blog-c1" and res.url.startswith("file:")


def test_simulated_comments_arrive_over_time(tmp_path: Path, clock) -> None:
    a = DryRunAdapter("threads", tmp_path / "preview", clock)
    ext = a.publish(_post(tmp_path)).external_id
    assert a.fetch_new_comments([ext], None) == []
    clock.advance(minutes=45)
    first = a.fetch_new_comments([ext], None)
    assert len(first) == 2
    since = max(c.created_at for c in first)
    clock.advance(hours=3)
    rest = a.fetch_new_comments([ext], since)
    assert len(rest) == 3 and all(c.created_at > since for c in rest)
    # 같은 입력 → 같은 결과(중복 처리 테스트가 가능)
    assert [c.external_id for c in a.fetch_new_comments([ext], None)] == [
        c.external_id for c in first + rest
    ]


def test_reply_and_hide_are_logged(tmp_path: Path, clock) -> None:
    preview = tmp_path / "preview"
    a = DryRunAdapter("threads", preview, clock)
    ext = a.publish(_post(tmp_path, "threads")).external_id
    clock.advance(hours=2)
    c = a.fetch_new_comments([ext], None)[0]
    a.reply(c, "고마워요")
    a.hide_comment(c)
    log = (preview / "actions.log").read_text("utf-8")
    assert "답글" in log and "숨김" in log and "고마워요" in log


def test_metrics_grow_and_follow_platform_matrix(tmp_path: Path, clock) -> None:
    a = DryRunAdapter("naver_blog", tmp_path / "preview", clock)
    ext = a.publish(_post(tmp_path, "naver_blog")).external_id
    m0 = a.read_post_metrics(ext)
    clock.advance(hours=48)
    m1 = a.read_post_metrics(ext)
    assert m1.views > m0.views
    assert m1.saves is None and m1.shares is None  # 블로그는 저장·공유 없음
    assert a.read_account_metrics().followers == 107
    clock.advance(days=1)
    assert a.read_post_metrics(ext).captured_at == clock.now()
    assert timedelta(0) == timedelta(0)


def test_preview_folder_uses_local_date(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from tests.fakes import FixedClock

    # 2026-10-11 23:30 UTC = 2026-10-12 08:30 KST
    clock = FixedClock(datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
    a = DryRunAdapter("threads", tmp_path / "preview", clock, tz="Asia/Seoul")
    a.publish(_post(tmp_path))
    assert (tmp_path / "preview" / "2026-10-12" / "c1" / "threads" / "post.txt").exists()


def test_instagram_cannot_hide_comments(tmp_path: Path, clock) -> None:
    """1일차 검증: 인스타그램 웹 댓글 메뉴에는 숨기기가 없다(ADR 0018)."""
    a = DryRunAdapter("instagram", tmp_path / "preview", clock)
    assert "hide_comment" not in a.capabilities()
    assert a.capabilities() == set(PLATFORM_CAPABILITIES["instagram"])
    ext = a.publish(_post(tmp_path, "instagram")).external_id
    clock.advance(hours=2)
    c = a.fetch_new_comments([ext], None)[0]
    with pytest.raises(NotSupportedError):
        a.hide_comment(c)
