"""배치 I 화면 동작: 오늘 할 일, 첫 설치 안내, 한국어 표시, 글꼴, 썸네일, 브랜드 색.

모양이 아니라 "마케터가 다음에 할 일을 알 수 있는가"를 검사한다(라우트·폼은 test_dashboard가 지킴).
"""

from __future__ import annotations

from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.domain.models import Content, ContentBrief
from nsm.store.repo import Repository
from tests.fakes import _ONE_PX_PNG, FakeTextGenerator, load_comment_cases, load_source_items


def _now(fn) -> Future:
    f: Future = Future()
    f.set_result(fn())
    return f


@pytest.fixture
def make(db, clock, app_config, tmp_path: Path):
    repo = Repository(db, clock)

    def build():
        state = AppState(
            repo=repo,
            clock=clock,
            get_config=lambda: app_config,
            set_config=lambda c: None,
            submit=_now,
            adapters={},
            media_dir=tmp_path / "media",
            reports_dir=tmp_path / "reports",
            run_job_now=lambda name: _now(lambda: name),
            job_names=frozenset({"collect"}),
        )
        return TestClient(create_app(state)), state

    def add(cid: str, status: str = "pending_review", hours_ago: int = 1):
        gen = FakeTextGenerator().generate_content(
            ContentBrief(topic=f"주제 {cid}", template="info", sources=load_source_items()[:1]),
            app_config.persona,
            app_config.brand,
        )
        repo.insert_content(
            Content(
                id=cid,
                status=status,
                generated=gen,
                relevance_confidence=0.9,
                created_at=clock.now() - timedelta(hours=hours_ago),
                review_reason="보호 모드",
            )
        )

    for item in load_source_items():
        repo.insert_source_item(item)
    return build, add, repo


def test_first_run_shows_steps_in_korean(make) -> None:
    build, _, _ = make
    client, _ = build()
    html = client.get("/").text
    assert "처음이시군요" in html and "자료 모으기 실행" in html
    assert 'action="/jobs/collect/run"' in html


def test_todo_lists_reviews_comments_and_opens_oldest_first(make, clock) -> None:
    build, add, repo = make
    add("new", hours_ago=1)
    add("old", hours_ago=5)
    comment, _ = load_comment_cases()[2]
    repo.insert_comment(comment)
    repo.update_comment(
        repo.comment_key(comment), status="escalated", category="complaint", reason="불만"
    )
    rid = repo.start_run("collect")
    repo.finish_run(rid, True, "ok")
    client, _ = build()
    html = client.get("/").text
    assert "검토할 원고" in html and 'href="/review/old"' in html  # 가장 오래 기다린 것부터
    assert "사람이 답할 댓글" in html
    assert "처음이시군요" not in html  # 실행 기록이 있으면 첫 설치 안내는 없다


def test_failed_publication_is_a_todo(make, clock) -> None:
    build, add, repo = make
    add("c1")
    pid = repo.create_publications("c1", ["threads"])[0]
    repo.transition_publication(pid, "publishing", increment_attempts=True)
    repo.transition_publication(
        pid,
        "failed",
        error="threads: 로그인 필요",
        next_attempt_at=clock.now() + timedelta(minutes=15),
    )
    html = build()[0].get("/").text
    assert "게시 실패" in html and "threads: 로그인 필요" in html and 'href="/review/c1"' in html


def test_nothing_to_do_says_so(make) -> None:
    build, _, repo = make
    rid = repo.start_run("collect")
    repo.finish_run(rid, True, "")
    assert "오늘 할 일이 없습니다" in build()[0].get("/").text


def test_internal_values_are_shown_in_korean(make) -> None:
    build, add, repo = make
    add("c1")
    rid = repo.start_run("generate")
    repo.finish_run(rid, False, "생성 실패")
    client, _ = build()
    detail = client.get("/review/c1").text
    assert "검토 대기" in detail and "정보형" in detail and "표지" in detail
    home = client.get("/").text
    assert "원고 만들기" in home and "자료 모으기" in home  # 잡 이름 + 설명
    assert ">generate<" not in home


def test_pretendard_is_served(make) -> None:
    r = make[0]()[0].get("/fonts/Pretendard-Regular.woff2")
    assert r.status_code == 200 and len(r.content) > 10_000


def test_review_list_shows_cover_thumbnail(make) -> None:
    build, add, _ = make
    add("c1")
    client, state = build()
    d = state.media_dir / "cards" / "c1"
    d.mkdir(parents=True)
    (d / "slide-01.png").write_bytes(_ONE_PX_PNG)
    html = client.get("/review").text
    assert 'src="/cards/c1/slide-01.png"' in html and "열어 보기" in html


def test_brand_color_is_the_accent(make, app_config) -> None:
    from nsm.render.html_renderer import palette

    html = make[0]()[0].get("/").text
    ink = palette(app_config.brand.primary_color, app_config.brand.accent_color)["primary_ink"]
    assert f"--accent: {ink}" in html


def test_discard_asks_for_confirmation(make) -> None:
    build, add, _ = make
    add("c1")
    html = build()[0].get("/review/c1").text
    assert "되돌릴 수 없습니다" in html and "저장하지 않은 수정이 있습니다" in html
