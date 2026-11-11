"""인스타그램 인사이트 글 해석(C44, ADR 0069). 실제 화면 글은 2026-11-11 시험 계정 글 하나에서 가져왔다."""

from __future__ import annotations

import pytest

from nsm.adapters.drivers.aside import AsideError
from nsm.adapters.platforms.aside_common import PlatformError
from nsm.adapters.platforms.instagram import InstagramAdapter, ig_number, parse_insights

# /insights/media/<id>/ 페이지 main의 innerText 원문(계정 정보 없음). 맨 앞 "0"은 조회수 제목 앞에 붙은 숫자,
# "홈 6 · 프로필 4"는 조회수가 나온 곳별 숫자다. 예전 정규식은 맨 앞 "0"을 조회수로, "게시물 반응 0"을 좋아요로 읽었다
REAL_2026_11 = """0
조회수
조회수
10
팔로워
100%
팔로워가 아닌 사람
0%
홈
6
프로필
4
조회한 사람
4
반응
반응
--
게시물 반응
0
좋아요
0
댓글
0
저장
0
공유
0
참여한 계정
--
프로필
프로필 활동
0
프로필 방문
0
외부 링크 누름
0
비즈니스 주소 누름
0
팔로우
0
광고
이 게시물 홍보하기"""


def test_real_screen_reads_name_then_number() -> None:
    assert parse_insights(REAL_2026_11) == {
        "views": 10,
        "reach": 4,
        "likes": 0,
        "comments": 0,
        "saves": 0,
        "shares": 0,
    }


def test_real_screen_with_nonzero_reactions() -> None:
    text = REAL_2026_11.replace(
        "게시물 반응\n0\n좋아요\n0\n댓글\n0", "게시물 반응\n5\n좋아요\n3\n댓글\n2"
    )
    got = parse_insights(text)
    assert (got["views"], got["likes"], got["comments"]) == (
        10,
        3,
        2,
    )  # 반응 합계(5)를 좋아요로 읽지 않음


def test_old_number_then_name_order() -> None:
    got = parse_insights("3\n조회수\n5\n좋아요\n1\n댓글\n0\n저장\n2\n공유")
    assert got == {"views": 3, "reach": None, "likes": 5, "comments": 1, "saves": 0, "shares": 2}


def test_same_line_pairs() -> None:
    got = parse_insights("조회수 1,234\n조회한 사람 1.2천\n좋아요 3만")
    assert (got["views"], got["reach"], got["likes"]) == (1234, 1200, 30000)


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1,234", 1234),
        ("1.2천", 1200),
        ("3만", 30000),
        ("1.5K", 1500),
        ("2M", 2_000_000),
        ("12", 12),
    ],
)
def test_units(raw: str, want: int) -> None:
    assert ig_number(raw) == want


def test_missing_items_are_none_not_zero() -> None:
    got = parse_insights("조회수\n7\n좋아요\n2")
    assert got == {
        "views": 7,
        "reach": None,
        "likes": 2,
        "comments": None,
        "saves": None,
        "shares": None,
    }
    assert parse_insights("")["views"] is None
    assert parse_insights("0")["views"] is None  # 여는 중에 먼저 보이는 "0"만 있는 글


def test_one_number_belongs_to_one_item() -> None:
    # "좋아요 4 댓글": 4는 좋아요 것이다. 댓글이 이름 앞 숫자로 4를 다시 집지 않는다
    got = parse_insights("조회수\n9\n좋아요\n4\n댓글")
    assert (got["likes"], got["comments"]) == (4, None)


def test_labels_must_match_whole_piece() -> None:
    got = parse_insights("조회수\n8\n공유하기\n3\n댓글 달기\n2")
    assert (got["shares"], got["comments"]) == (None, None)


def test_percent_is_not_a_number() -> None:
    assert parse_insights("조회수\n100%\n5")["views"] is None


class _Repl:
    def __init__(self, data) -> None:
        self.data = data

    def available(self) -> bool:
        return True

    def run(self, body, args=None, files=None):
        if isinstance(self.data, AsideError):
            raise self.data
        return self.data


def _adapter(clock, data) -> InstagramAdapter:
    return InstagramAdapter("@nonsleepmarketer", clock, repl=_Repl(data))


def test_adapter_parses_raw_text(clock) -> None:
    m = _adapter(clock, {"text": REAL_2026_11, "insights": True, "comments": 3}).read_post_metrics(
        "P1"
    )
    assert (m.views, m.reach, m.likes, m.comments, m.saves, m.shares) == (10, 4, 0, 0, 0, 0)


def test_adapter_falls_back_to_dom_comment_count(clock) -> None:
    m = _adapter(clock, {"text": "조회수\n7\n좋아요\n2", "comments": 3}).read_post_metrics("P1")
    assert (m.views, m.likes, m.comments, m.saves) == (7, 2, 3, None)


def test_views_missing_fails_and_saves_evidence(clock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NSM_HOME", str(tmp_path))
    page = {"url": "https://www.instagram.com/insights/media/1/", "text": "0"}
    a = _adapter(clock, {"text": "", "insights": False, "comments": 0, "page": page})
    with pytest.raises(PlatformError) as ei:
        a.read_post_metrics("P1")
    assert ei.value.kind == "ui_changed"
    assert "조회수를 못 찾음" in str(ei.value)
    assert ei.value.evidence_dir and "증거:" in str(ei.value)


def test_reach_without_views_is_impossible(clock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NSM_HOME", str(tmp_path))
    a = _adapter(clock, {"text": "조회수\n0\n조회한 사람\n4\n좋아요\n0", "comments": 0})
    with pytest.raises(PlatformError) as ei:
        a.read_post_metrics("P1")
    assert ei.value.kind == "ui_changed"
    assert "조회한 사람 4" in str(ei.value)


def test_zero_views_with_zero_reach_is_kept(clock) -> None:
    m = _adapter(clock, {"text": "조회수\n0\n조회한 사람\n0", "comments": 0}).read_post_metrics(
        "P1"
    )
    assert (m.views, m.reach) == (0, 0)
