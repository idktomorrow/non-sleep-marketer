"""대시보드 라우트 테스트 (S6 #22). 화면 모양이 아니라 동작(상태 변화)을 검사한다.

배치 I에서 화면을 바꿔도 이 테스트가 통과하면 기능은 그대로다.
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.domain.models import Content, ContentBrief
from nsm.engine.generate import AUTO_PUBLISH_KEY
from nsm.store.repo import Repository
from tests.fakes import (
    FakePlatformAdapter,
    FakeTextGenerator,
    load_comment_cases,
    load_source_items,
)


def _run_now(fn) -> Future:
    f: Future = Future()
    try:
        f.set_result(fn())
    except Exception as e:  # noqa: BLE001
        f.set_exception(e)
    return f


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    repo = Repository(db, clock)
    holder = {"cfg": app_config}
    saved: list = []

    def set_config(c):
        saved.append(c)
        holder["cfg"] = c

    adapters = {"threads": FakePlatformAdapter("threads", clock=clock)}
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: holder["cfg"],
        set_config=set_config,
        submit=_run_now,
        adapters=adapters,
        media_dir=tmp_path / "media",
        reports_dir=tmp_path / "reports",
        run_job_now=lambda name: _run_now(lambda: name),
        job_names=frozenset({"collect", "publish"}),
    )
    client = TestClient(create_app(state))
    for item in load_source_items():
        repo.insert_source_item(item)
    gen = FakeTextGenerator(inject_text="무조건 좋아요").generate_content(
        ContentBrief(topic="핸드드립", template="info", sources=load_source_items()[:2]),
        app_config.persona,
        app_config.brand,
    )
    repo.insert_content(
        Content(
            id="c1",
            status="pending_review",
            generated=gen,
            relevance_confidence=0.9,
            created_at=clock.now(),
            review_reason="자동 게시가 꺼져 있음",
        )
    )
    repo.create_publications("c1", ["threads", "instagram"])
    return {
        "client": client,
        "repo": repo,
        "state": state,
        "holder": holder,
        "saved": saved,
        "adapters": adapters,
    }


def _clean(env) -> None:
    """픽스처 원고의 금칙어를 고친다(금칙어가 있으면 승인할 수 없으므로)."""
    _post(
        env,
        "/review/c1/edit",
        instagram_caption="캡션",
        threads_text="스레드",
        blog_title="제목",
        blog_markdown="본문",
        cover_title="표지",
    )


def _post(env, url: str, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, follow_redirects=False
    )


@pytest.mark.parametrize(
    "path", ["/", "/review", "/review/c1", "/comments", "/performance", "/logs", "/healthz"]
)
def test_pages_render(env, path: str) -> None:
    r = env["client"].get(path)
    assert r.status_code == 200


def test_review_page_shows_reason_and_banned_words(env) -> None:
    html = env["client"].get("/review/c1").text
    assert "자동 게시가 꺼져 있음" in html and "무조건" in html


def test_csrf_required(env) -> None:
    r = env["client"].post("/review/c1/approve", data={"csrf": "wrong"})
    assert r.status_code == 403
    assert env["repo"].get_content("c1").status == "pending_review"


def test_approve_by_human_and_requeue(env) -> None:
    repo = env["repo"]
    pubs = repo.publications_for_content("c1")
    repo.db.execute("UPDATE publications SET status = 'abandoned' WHERE id = ?", [pubs[0].id])
    _clean(env)
    r = _post(env, "/review/c1/approve")
    assert r.status_code == 303
    assert repo.get_content("c1").status == "approved"
    assert repo.count_human_approved() == 1
    assert all(p.status == "queued" for p in repo.publications_for_content("c1"))
    assert _post(env, "/review/c1/approve").status_code == 409  # 두 번 승인 불가


def test_discard(env) -> None:
    _post(env, "/review/c1/discard")
    assert env["repo"].get_content("c1").status == "discarded"


def test_edit_updates_text(env, tmp_path: Path) -> None:
    cards = env["state"].media_dir / "cards" / "c1"
    cards.mkdir(parents=True)
    (cards / "slide-01.png").write_bytes(b"x")
    r = _post(
        env,
        "/review/c1/edit",
        instagram_caption="새 캡션",
        threads_text="새 스레드",
        blog_title="새 제목",
        blog_markdown="새 본문",
        cover_title="새 표지",
    )
    assert r.status_code == 303
    g = env["repo"].get_content("c1").generated
    assert g.instagram_caption == "새 캡션" and g.deck.slides[0].title == "새 표지"
    assert not (cards / "slide-01.png").exists()  # 옛 렌더 삭제


def test_card_image_route_blocks_traversal(env) -> None:
    assert env["client"].get("/cards/c1/..%5Cconfig.yaml").status_code == 404
    assert env["client"].get("/cards/c1/slide-99.png").status_code == 404


def test_toggles(env) -> None:
    before = env["holder"]["cfg"].persona.enabled
    _post(env, "/persona/toggle")
    assert env["holder"]["cfg"].persona.enabled is (not before)
    assert env["saved"], "설정 파일 저장이 호출돼야 한다"
    _post(env, "/auto-publish/toggle")
    assert env["repo"].get_setting(AUTO_PUBLISH_KEY) is True


def test_escalated_comment_reply_and_resolve(env) -> None:
    repo = env["repo"]
    comment, _ = load_comment_cases()[2]  # 불만
    comment = comment.model_copy(update={"platform": "threads"})
    repo.insert_comment(comment)
    key = repo.comment_key(comment)
    repo.update_comment(key, status="escalated", reason="불만")
    assert comment.text in env["client"].get("/comments").text
    r = _post(env, f"/comments/{key}/reply", text="불편을 드려 죄송합니다. DM 주세요")
    assert r.status_code == 303
    s = repo.get_comment(key)
    assert s.status == "resolved" and s.handled_by == "human"
    assert env["adapters"]["threads"].replied[0][1].startswith("불편을")

    other, _ = load_comment_cases()[4]
    repo.insert_comment(other)
    _post(env, f"/comments/{repo.comment_key(other)}/resolve")
    assert repo.get_comment(repo.comment_key(other)).status == "resolved"


def test_reply_failure_reports_502(env) -> None:
    repo = env["repo"]
    comment, _ = load_comment_cases()[1]
    repo.insert_comment(comment)
    key = repo.comment_key(comment)
    repo.update_comment(key, status="escalated", reason="x")
    env["adapters"]["threads"] = FakePlatformAdapter("threads", capabilities=set())
    r = _post(env, f"/comments/{key}/reply", text="x")
    assert r.status_code == 502
    assert repo.get_comment(key).status == "escalated"  # 실패하면 다시 사람 확인 목록으로


def test_run_job_now(env) -> None:
    assert _post(env, "/jobs/collect/run").status_code == 303


def test_approve_blocked_when_banned_words(env) -> None:
    r = _post(env, "/review/c1/approve")
    assert r.status_code == 409 and "금칙어" in r.text


def test_approve_after_fixing_banned_words(env) -> None:
    _post(
        env,
        "/review/c1/edit",
        instagram_caption="캡션",
        threads_text="스레드",
        blog_title="제목",
        blog_markdown="본문",
        cover_title="표지",
    )
    assert _post(env, "/review/c1/approve").status_code == 303


def test_unknown_host_rejected(env) -> None:
    r = env["client"].get("/", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_javascript_urls_not_linked(env) -> None:
    repo = env["repo"]
    repo.db.execute("UPDATE source_items SET url = 'javascript:alert(1)'")
    html = env["client"].get("/review/c1").text
    assert 'href="javascript:' not in html


def test_unknown_job_404(env) -> None:
    assert _post(env, "/jobs/nope/run").status_code == 404


def test_reply_not_sent_twice(env) -> None:
    repo = env["repo"]
    comment, _ = load_comment_cases()[1]
    repo.insert_comment(comment)
    key = repo.comment_key(comment)
    repo.update_comment(key, status="escalated", reason="x")
    assert _post(env, f"/comments/{key}/reply", text="답").status_code == 303
    assert _post(env, f"/comments/{key}/reply", text="답").status_code == 409
    assert len(env["adapters"]["threads"].replied) == 1


def test_reports_page(env) -> None:
    d = env["state"].reports_dir
    assert "아직 리포트가 없습니다" in env["client"].get("/reports").text
    d.mkdir(parents=True)
    (d / "weekly-2026-10-05.md").write_text("# 주간 리포트 지난주", encoding="utf-8")
    (d / "weekly-2026-10-12.md").write_text("# 주간 리포트 이번주", encoding="utf-8")
    assert "이번주" in env["client"].get("/reports").text  # 최신이 기본
    assert "지난주" in env["client"].get("/reports?name=weekly-2026-10-05.md").text
    assert env["client"].get("/reports?name=..%2Fconfig.yaml").status_code == 404


def test_reports_link_hidden_without_reports_dir(env) -> None:
    assert 'href="/reports"' in env["client"].get("/").text
    env["state"].reports_dir = None
    from fastapi.testclient import TestClient

    from nsm.app.server import create_app

    html = TestClient(create_app(env["state"])).get("/").text
    assert 'href="/reports"' not in html


def test_settings_presets_panel_and_actions(env, tmp_path: Path) -> None:
    """C29: 프리셋 칸은 전환 함수가 있을 때만. 저장은 파일, 전환은 작업 스레드(Future), 삭제는 보호 규칙."""
    from nsm.config.presets import PresetStore

    html = env["client"].get("/settings").text
    assert "프리셋" not in html.split("<h2>페르소나</h2>")[0]
    store = PresetStore(tmp_path / "presets", tmp_path / "data.duckdb")
    switched: list[str] = []

    def switch(name: str) -> Future:
        switched.append(name)
        f: Future = Future()
        f.set_result(name)
        return f

    env["state"].presets = store
    env["state"].switch_preset = switch
    html = env["client"].get("/settings").text
    assert 'id="presets"' in html and "아직 저장 안 됨" in html
    assert _post(env, "/settings/preset/save", name="카페").status_code == 303
    assert store.exists("카페")
    html = env["client"].get("/settings").text
    assert "카페" in html and "전환" in html
    assert _post(env, "/settings/preset/switch", name="카페").status_code == 303
    assert switched == ["카페"]
    assert (
        _post(env, "/settings/preset/switch", name="없는것")
        .headers["location"]
        .startswith("/settings?preset_error=")
    )
    # 빈 이름·점만 있는 이름은 500이 아니라 안내로
    assert "preset_error" in _post(env, "/settings/preset/switch", name="...").headers["location"]
    # 다른 기존 프리셋은 덮어쓰지 않는다
    assert "preset_error" in _post(env, "/settings/preset/save", name="카페").headers["location"]
    assert _post(env, "/settings/preset/delete", name="카페").status_code == 303
    assert not store.exists("카페")
    assert "preset_error" in _post(env, "/settings/preset/delete", name="기본").headers["location"]


def test_footer_shows_running_version(env) -> None:
    """C26: 화면 맨 아래 버전 번호. 사용자가 머지 뒤 다시 켰는지 이 번호로 안다."""
    import re

    html = env["client"].get("/").text
    assert re.search(r"논슬립 마케터 v\d+\.\d+", html)


def test_overview_shows_next_publish_time_when_waiting(env) -> None:
    """C26: 승인된 원고가 있으면 "게시 대기" 칸에 플랫폼별 다음 게시 시각."""
    html = env["client"].get("/").text
    assert "활동 시간에 올라갑니다" in html and "다음 게시" not in html
    _clean(env)
    assert _post(env, "/review/c1/approve").status_code == 303
    html = env["client"].get("/").text
    assert "다음 게시" in html and ("스레드" in html or "인스타그램" in html)
    # C28: 플랫폼+시각은 한 덩어리(span)로 감싸 줄이 단어 중간에서 끊기지 않게 한다
    assert 'class="muted small next-pubs">다음 게시 <span>' in html
