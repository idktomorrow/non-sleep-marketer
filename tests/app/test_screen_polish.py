"""화면 정리 2차(C45): 성과 요약 카드·접힌 표, 설정 바로가기, 오늘 화면 배치·문구, 실패 요약, 단계 이름, 휴대폰 표."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

import nsm.app.server as app_module
from nsm.app.ko import FILTERS
from nsm.app.perf_summary import followers_card, summary_cards
from nsm.domain.models import AccountMetrics
from nsm.engine.summary import describe, friendly_error, readable
from tests.app.test_dashboard import env  # noqa: F401  같은 대시보드 픽스처
from tests.fakes import load_comment_cases

D = date(2026, 11, 13)


def _f(pl: str, days_ago: int, n: int) -> dict:
    return {"platform": pl, "day": D - timedelta(days=days_ago), "followers": n}


# ------------------------------------------------------------------ 성과 요약 카드


def test_followers_card_change_against_seven_days_before() -> None:
    rows = [_f("threads", 9, 90), _f("threads", 7, 100), _f("threads", 0, 104)]
    rows += [
        _f("instagram", 8, 50),
        _f("instagram", 0, 48),
    ]  # 7일 전 기록 없음 → 그 전 가장 가까운 날
    out = followers_card(rows)
    assert [c["name"] for c in out] == ["인스타그램", "스레드"]  # 인스타그램 먼저
    assert out[0] == {"name": "인스타그램", "now": 48, "delta": -2}
    assert out[1] == {"name": "스레드", "now": 104, "delta": 4}


def test_followers_card_without_old_value_has_no_change() -> None:
    out = followers_card([_f("threads", 3, 10), _f("threads", 0, 12)])
    assert out == [{"name": "스레드", "now": 12, "delta": None}]
    assert followers_card([]) == []


def test_summary_cards_zero_when_nothing() -> None:
    s = summary_cards([], [], {"by_status": {}, "avg_response_minutes": None})
    assert s == {"followers": [], "cost_usd": 0, "auto_replied": 0, "human": 0, "avg_minutes": None}
    s = summary_cards(
        [],
        [{"usd": 0.12}, {"usd": 0.031}],
        {"by_status": {"auto_replied": 5, "resolved": 2, "hidden": 1}, "avg_response_minutes": 3.5},
    )
    assert s["cost_usd"] == 0.151 and (s["auto_replied"], s["human"], s["avg_minutes"]) == (
        5,
        2,
        3.5,
    )


def test_performance_page_summary_and_folded_tables(env, clock) -> None:  # noqa: F811
    repo = env["repo"]
    for days, n in ((7, 100), (0, 104)):
        repo.insert_account_metrics(
            AccountMetrics(
                platform="threads", captured_at=clock.now() - timedelta(days=days), followers=n
            )
        )
    repo.record_cost("llm", "anthropic", 0.25, "x")
    html = env["client"].get("/performance").text
    summary = html[html.index('class="stats stats-row perf-summary"') : html.index("게시물별")]
    assert "스레드 104" in summary and "7일 전보다 스레드 +4" in summary
    assert "최근 14일 비용" in summary and "$0.25" in summary
    assert "자동 답글 0 · 사람 처리 0" in summary  # 댓글이 없으면 0
    # 날짜별 팔로워·비용 표는 접혀 있다
    assert html.count('<details class="fold"><summary>날짜별 보기') == 2
    assert re.search(
        r'<details class="fold">(?:(?!</details>).)*<th>날짜</th><th>플랫폼</th>', html, re.S
    )


def test_performance_page_without_records(env) -> None:  # noqa: F811
    html = env["client"].get("/performance").text
    assert "아직 기록이 없습니다" in html and "7일 전보다" not in html and "$0.00" in html


# ------------------------------------------------------------------ 설정 바로가기


def test_settings_jump_links_match_section_ids(env) -> None:  # noqa: F811
    html = env["client"].get("/settings").text
    nav = re.search(r'<nav class="jump[^"]*"[^>]*>(.*?)</nav>', html, re.S).group(1)
    targets = re.findall(r'href="#([\w-]+)"', nav)
    assert {"persona", "brand", "ops", "alerts", "doc-index"} <= set(targets)
    for t in targets:
        assert f'<h2 id="{t}"' in html, t
    for kept in (
        "api-keys",
        "doc-index",
        "brand-images",
        "presets",
    ):  # 다른 화면이 링크하는 id는 그대로
        if f"#{kept}" in targets:
            assert f'id="{kept}"' in html
    assert len(re.findall(r"<h2(?! id=)", html)) == 0  # 모든 절에 id


# ------------------------------------------------------------------ 오늘 화면


@pytest.mark.parametrize("mode", ["simple", "advanced"])
def test_today_layout_and_stat_cards(env, mode) -> None:  # noqa: F811
    from tests.app.test_dashboard import _post

    _post(env, "/ui-mode", mode=mode)
    html = env["client"].get("/").text
    assert '<div class="today">' in html and '<div class="stats stats-row">' in html
    stats = html[html.index('<div class="stats stats-row">') : html.index('class="panel switches"')]
    assert stats.count('<div class="stat"') == (3 if mode == "simple" else 4)
    assert "grid-column: 1 / -1" not in stats  # 한 칸짜리 카드만(한 줄에 놓인다)
    assert html.index('class="stats stats-row"') < html.index(
        "<b>페르소나</b>"
    )  # 스위치는 숫자 카드 다음


def test_protect_mode_text(env, monkeypatch) -> None:  # noqa: F811
    protect = env["holder"]["cfg"].settings.protect_count
    monkeypatch.setattr(env["repo"], "count_human_approved", lambda: 3)
    html = env["client"].get("/").text
    assert f"보호 모드: 사람 승인 3/{protect}건(모두 검토로)" in html and "보호 모드 끝" not in html


def test_protect_mode_done_text(env, monkeypatch) -> None:  # noqa: F811
    done = (
        env["holder"]["cfg"].settings.protect_count + 1
    )  # 예: 11/10건처럼 넘친 숫자를 보이지 않는다
    monkeypatch.setattr(env["repo"], "count_human_approved", lambda: done)
    html = env["client"].get("/").text
    assert f"보호 모드 끝(사람 승인 {done}건)" in html and f"{done}/" not in html


# ------------------------------------------------------------------ 실패 요약 문구


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (
            "PlatformError: threads: 게시를 확인하지 못함(프로필에서 글을 못 찾음) (증거: threads-20261113-163107)",
            "스레드: 게시를 확인하지 못함(프로필에서 글을 못 찾음)",
        ),
        ("LoginRequiredError: instagram: 로그인 필요", "인스타그램: 로그인 필요"),
        ("threads: 요청 제한", "스레드: 요청 제한"),
        ("Aside 응답 없음", "Aside 응답 없음"),  # 모르는 모양은 그대로
        ("ReadTimeout: 120초 넘음", "120초 넘음"),
    ],
)
def test_friendly_error(raw: str, want: str) -> None:
    assert friendly_error(raw) == want


def test_describe_and_old_records_use_friendly_error() -> None:
    err = "PlatformError: threads: 게시를 확인하지 못함 (증거: threads-1)"
    text = describe("publish", counts={}, errors=[err])
    assert text.endswith("오류 1건: 스레드: 게시를 확인하지 못함") and "증거" not in text
    # C45 전에 문장 형식으로 남은 기록
    old = "게시 0건 · 오류 2건: PlatformError: instagram: 이미지 없음 (증거: ig-2)"
    assert readable("publish", old) == "게시 0건 · 오류 2건: 인스타그램: 이미지 없음"
    # 더 옛 형식(reply: k=v / 오류 n건: …)도 깨지지 않는다
    legacy = readable("reply", "reply: new=0 / 오류 1건: PlatformError: threads: 끊김")
    assert legacy.endswith("오류 1건: 스레드: 끊김")
    assert readable("collect", "그냥 문장") == "그냥 문장" and readable("collect", None) == ""


# ------------------------------------------------------------------ 장애 단계 이름


def test_ko_stage() -> None:
    ko = FILTERS["ko_stage"]
    assert [
        ko(s) for s in ("publish", "fetch_comments", "health", "reply", "metrics", "login")
    ] == [
        "게시",
        "댓글 읽기",
        "로그인 점검",
        "답글",
        "성과 읽기",
        "로그인",
    ]
    assert ko("new_stage") == "new_stage" and ko(None) == ""


def test_incidents_page_shows_korean_stage_with_original_in_title(env) -> None:  # noqa: F811
    env["repo"].record_failure(
        platform="threads",
        job="reply",
        stage="fetch_comments",
        kind="transient",
        message="끊김",
        evidence_dir=None,
        action="재시도",
    )
    html = env["client"].get("/incidents").text
    assert '<span title="fetch_comments">댓글 읽기</span>' in html


# ------------------------------------------------------------------ 휴대폰 표(세로로 쌓기)


def _stack_tables(html: str) -> list[str]:
    return re.findall(r'<table class="stack">(.*?)</table>', html, re.S)


def _check_labels(table: str) -> None:
    head = re.search(r"<thead>(.*?)</thead>", table, re.S).group(1)
    ths = re.findall(r"<th[ >]", head)
    rows = re.findall(r"<tr>(.*?)</tr>", table.split("</thead>", 1)[1], re.S)
    assert rows, "행이 있어야 검사할 수 있다"
    for row in rows:
        tds = re.findall(r"<td[ >]", row)
        labels = re.findall(r'<td[^>]*data-label="[^"]+"', row)
        assert len(tds) == len(ths) == len(labels)


def test_comments_table_stacks_with_labels(env) -> None:  # noqa: F811
    comment, _ = load_comment_cases()[1]
    env["repo"].insert_comment(comment)
    [table] = _stack_tables(env["client"].get("/comments").text)
    _check_labels(table)


def test_logs_table_stacks_with_labels(env) -> None:  # noqa: F811
    rid = env["repo"].start_run("collect")
    env["repo"].finish_run(rid, False, "오류 1건: 끊김")
    [table] = _stack_tables(env["client"].get("/logs?all=1").text)
    _check_labels(table)


def test_performance_posts_table_stacks_with_labels(env) -> None:  # noqa: F811
    repo = env["repo"]
    pid = repo.db.execute("SELECT id FROM publications WHERE platform = 'threads'")[0][0]
    repo.transition_publication(pid, "publishing", increment_attempts=True)
    repo.transition_publication(pid, "published", external_id="t1", url="https://example.com/t1")
    [table] = _stack_tables(env["client"].get("/performance").text)
    _check_labels(table)


def test_other_tables_keep_plain_desktop_markup(env) -> None:  # noqa: F811
    """접는 표·나머지 표는 그대로(.table-wrap 안의 보통 표). 옆으로 미는 안내는 JS가 휴대폰에서만 붙인다."""
    html = env["client"].get("/logs").text
    assert '<table class="job-status">' in html and "has-more" in html  # 클래스는 JS가 토글
    css = (Path(app_module.__file__).parent / "static" / "app.css").read_text("utf-8")
    assert "@media (max-width: 720px)" in css and "td::before { content: attr(data-label)" in css
