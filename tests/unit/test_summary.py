"""작업 결과 요약 문장 (배치 C10): 실행 기록·지금 실행 알림·로그가 같은 한국어 문장을 쓴다."""

from __future__ import annotations

import pytest

from nsm.engine.context import JobResult
from nsm.engine.summary import describe, readable

WAIT = "인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"


@pytest.mark.parametrize(
    ("job", "counts", "text"),
    [
        (
            "reply",
            {
                "new": 0,
                "auto_replied": 0,
                "hidden": 0,
                "escalated": 0,
                "ignored": 0,
                "rate_limited": [WAIT],
            },
            f"새 댓글 없음 · {WAIT}",
        ),
        (
            "reply",
            {"new": 3, "auto_replied": 2, "hidden": 0, "escalated": 1, "ignored": 0},
            "새 댓글 3개 · 자동 답글 2개, 사람 확인으로 넘김 1개",
        ),
        ("health", {"paused": []}, "로그인 정상"),
        ("health", {"paused": ["instagram"]}, "로그인이 필요해 멈춘 곳: 인스타그램"),
        (
            "publish",
            {"published": 0, "failed": 0, "waits": ["스레드 3시간 간격 대기(14:00 이후)"]},
            "올린 글 없음 · 이유: 스레드 3시간 간격 대기(14:00 이후)",
        ),
        ("publish", {"published": 1, "failed": 1}, "1건 올림, 1건 실패"),
        (
            "collect",
            {"new": 30, "duplicate": 12, "classified": 30, "relevant": 4, "bodies": 3},
            "새 자료 30개(이미 본 자료 12개 뺌) · 30개를 살펴 관련 자료 4개 · 원문 3개 읽음",
        ),
        (
            "generate",
            {"created": 1, "pending_review": 1, "approved": 0, "templates": ["list"]},
            "원고 1개 만듦(검토 대기 1개) · 형식: 리스트형",
        ),
        ("metrics", {"posts": 5, "accounts": 2}, "게시물 지표 5개, 계정 지표 2개 읽음"),
        (
            "weekly_report",
            {"posts": 9, "file": "weekly-2026-10-12.md"},
            "지난 7일 게시물 9개 정리 · 파일 weekly-2026-10-12.md",
        ),
    ],
)
def test_each_job_reads_as_a_sentence(job: str, counts: dict, text: str) -> None:
    assert describe(job, counts=counts) == text
    assert JobResult(job, counts=counts).summary() == text
    assert "=" not in text and "[" not in text  # 내부 값 표기가 새지 않는다


def test_skipped_and_errors() -> None:
    assert JobResult("publish", skipped="게시 시간 밖").summary() == "건너뜀: 게시 시간 밖"
    s = JobResult("reply", counts={"new": 0}, errors=["instagram 댓글 조회 실패: x", "y"]).summary()
    assert s == "새 댓글 없음 · 오류 2건: instagram 댓글 조회 실패: x"


@pytest.mark.parametrize(
    ("job", "old", "new"),
    [
        (
            "reply",
            "reply: new=0, auto_replied=0, hidden=0, escalated=0, ignored=0, rate_limited=['인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)']",
            f"새 댓글 없음 · {WAIT}",
        ),
        (
            "reply",
            "reply: new=0, auto_replied=0, hidden=0, escalated=0, ignored=0 / 오류 1건: instagram 댓글 조회 실패: instagram 로그인 필요: 로그인돼 있지 않음",
            "새 댓글 없음 · 오류 1건: instagram 댓글 조회 실패: instagram 로그인 필요: 로그인돼 있지 않음",
        ),
        ("health", "health: paused=[]", "로그인 정상"),
        ("publish", "publish: 건너뜀 (게시 시간 밖)", "건너뜀: 게시 시간 밖"),
        (
            "publish",
            "publish: published=0, failed=0, waits=['인스타그램 오늘 1건 다 올림(내일 다시)', '스레드 3시간 간격 대기(14:00 이후)']",
            "올린 글 없음 · 이유: 인스타그램 오늘 1건 다 올림(내일 다시), 스레드 3시간 간격 대기(14:00 이후)",
        ),
        ("generate", "generate: 변화 없음", "새 원고 없음"),
    ],
)
def test_old_log_rows_are_shown_as_sentences(job: str, old: str, new: str) -> None:
    assert readable(job, old) == new


def test_unknown_messages_are_left_alone() -> None:
    assert readable("generate", "RuntimeError: boom") == "RuntimeError: boom"
    assert readable("reply", "새 댓글 없음") == "새 댓글 없음"  # 이미 새 형식
    assert readable("reply", None) == ""


def test_publish_failure_only() -> None:
    c = {"published": 0, "failed": 1, "waits": ["스레드 3시간 간격 대기(23:24 이후)"]}
    assert (
        describe("publish", counts=c) == "1건 실패 · 안 올린 곳: 스레드 3시간 간격 대기(23:24 이후)"
    )


def test_tricky_old_rows() -> None:
    # 따옴표 없는 글자 값(예전 f"{k}={v}")
    assert readable("weekly_report", "weekly_report: posts=9, file=weekly-2026-10-12.md") == (
        "지난 7일 게시물 9개 정리 · 파일 weekly-2026-10-12.md"
    )
    # 목록 글자 안의 ", x="
    old = "publish: published=1, failed=0, waits=['스레드 a, b=c', '인스타그램 d']"
    assert readable("publish", old) == "1건 올림 · 안 올린 곳: 스레드 a, b=c, 인스타그램 d"


def test_leftover_comments_handled_without_new_ones() -> None:
    """지난번 처리하다 멈춘 댓글을 이번에 처리: "새 댓글 없음 · 자동 답글 2개"로 헷갈리게 쓰지 않는다."""
    assert describe("reply", counts={"new": 0, "auto_replied": 2}) == (
        "새 댓글 없음, 지난번 남은 댓글 처리 · 자동 답글 2개"
    )


def test_every_job_has_a_sentence() -> None:
    from nsm.engine.runner import JOBS
    from nsm.engine.summary import _JOBS

    assert set(JOBS) <= set(_JOBS)  # 새 잡을 더하면 문장도 더한다
    assert describe("new_job", counts={"waits": ["a", "b"], "n": 1}) == "waits a, b · n 1"
