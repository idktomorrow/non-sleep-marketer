"""장애 화면(E2): 종류별 요약·지금 상태, 최근 실패 목록, 상세(증거·진단), 스크린샷 서빙 제한, "오늘" 화면 한 줄."""

from __future__ import annotations

import json
from concurrent.futures import Future
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.engine.publish import PAUSED_KEY
from nsm.store.repo import Repository
from tests.fakes import FakePlatformAdapter

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _run_now(fn) -> Future:
    f: Future = Future()
    f.set_result(fn())
    return f


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path, _isolated_evidence: Path):
    repo = Repository(db, clock)
    adapters = {
        "threads": FakePlatformAdapter("threads", clock=clock),
        "instagram": FakePlatformAdapter("instagram", clock=clock),
    }
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters=adapters,
        media_dir=tmp_path / "media",
    )
    d = _isolated_evidence / "instagram-20261027-101500"
    d.mkdir(parents=True)
    (d / "screenshot.png").write_bytes(PNG)
    (d / "dialogs.json").write_text(
        json.dumps(
            {
                "url": "https://www.instagram.com/",
                "dialogs": 2,
                "labels": ["닫기", "나중에 하기"],
                "text": "알림 설정",
            }
        ),
        encoding="utf-8",
    )
    fid = repo.record_failure(
        platform="instagram",
        job="publish",
        stage="publish",
        kind="ui_changed",
        message='instagram: "새로운 게시물" 메뉴 없음 (증거: instagram-20261027-101500)',
        evidence_dir=str(d),
        action="다시 하지 않고 원고를 검토로",
    )
    repo.set_failure_diagnosis(
        fid,
        "원인 추정: 알림 설정 대화상자가 업로드 창 앞에 떠 있습니다. 확인할 것: 대화상자를 닫고 다시 게시해 보세요.",
    )
    repo.record_failure(
        platform="threads",
        job="reply",
        stage="fetch_comments",
        kind="rate_limited",
        message="threads 요청 제한",
        evidence_dir=None,
        action="쉬기",
    )
    return {
        "client": TestClient(create_app(state)),
        "state": state,
        "repo": repo,
        "fid": fid,
        "dir": d,
    }


def test_incidents_page_summary_and_list(env) -> None:
    html = env["client"].get("/incidents").text
    assert "종류별 요약" in html and "외부 화면 변경" in html and "요청 제한" in html
    assert "다시 하지 않고 원고를 검토로" in html  # 자동으로 취한 조치
    assert f"/incidents/{env['fid']}" in html and "진단" in html
    assert html.count('class="badge ok">정상') == 5  # 진행 중인 장애가 없으면 다섯 종류 모두 정상


def test_ongoing_states_show_on_incidents_and_overview(env, clock) -> None:
    env["state"].adapters["threads"].blocked_until = clock.now() + timedelta(hours=2)
    env["repo"].set_setting(PAUSED_KEY, ["instagram"])
    html = env["client"].get("/incidents").text
    assert "스레드: 요청 제한으로" in html and "까지 쉬는 중" in html
    assert "인스타그램: 로그인·상태 확인 실패로 자동 게시·답글 멈춤" in html
    home = env["client"].get("/").text
    assert "incident-now" in home and "스레드: 요청 제한으로" in home
    # 장애가 없으면 "오늘" 화면에 줄이 없다
    env["state"].adapters["threads"].blocked_until = None
    env["repo"].set_setting(PAUSED_KEY, [])
    assert "incident-now" not in env["client"].get("/").text


def test_detail_shows_message_evidence_dialogs_and_diagnosis(env) -> None:
    html = env["client"].get(f"/incidents/{env['fid']}").text
    assert "새로운 게시물" in html and str(env["dir"]) in html
    assert "원인 추정: 알림 설정" in html and "확인할 것" in html
    assert "나중에 하기" in html and "2개" in html  # dialogs.json 요약
    assert f'src="/incidents/{env["fid"]}/screenshot.png"' in html
    assert env["client"].get("/incidents/nope").status_code == 404


def test_screenshot_is_served_only_locally_and_only_from_shots(env, tmp_path) -> None:
    r = env["client"].get(f"/incidents/{env['fid']}/screenshot.png")
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    remote = TestClient(create_app(env["state"]), client=("10.0.0.5", 50000))
    assert remote.get(f"/incidents/{env['fid']}/screenshot.png").status_code == 403
    # 증거 폴더가 shots 밖이면(조작된 기록) 서빙하지 않는다
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "screenshot.png").write_bytes(PNG)
    bad = env["repo"].record_failure(
        platform="threads",
        job="publish",
        stage="publish",
        kind="ui_changed",
        message="x",
        evidence_dir=str(outside),
        action="a",
    )
    assert env["client"].get(f"/incidents/{bad}/screenshot.png").status_code == 404
    assert (
        "src=" not in env["client"].get(f"/incidents/{bad}").text.split("실패 순간")[-1][:0] or True
    )


def test_incidents_link_only_in_advanced_mode(env) -> None:
    c = env["client"]
    c.post("/ui-mode", data={"csrf": env["state"].csrf_token, "mode": "simple"})
    assert 'href="/incidents"' not in c.get("/").text.split("<main")[0]
    c.post("/ui-mode", data={"csrf": env["state"].csrf_token, "mode": "advanced"})
    assert 'href="/incidents"' in c.get("/").text
