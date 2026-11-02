"""E5: 오늘·장애 화면이 로그인 세부 상태와 사람이 할 일을 보여 준다."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.engine.pause import PAUSE_INFO_KEY
from nsm.engine.publish import PAUSED_KEY
from nsm.store.repo import Repository
from tests.fakes import FakePlatformAdapter


def _run_now(fn) -> Future:
    f: Future = Future()
    f.set_result(fn())
    return f


def test_overview_and_incidents_show_state_and_todo(db, clock, app_config, tmp_path: Path) -> None:
    repo = Repository(db, clock)
    repo.set_setting(PAUSED_KEY, ["instagram"])
    repo.set_setting(
        PAUSE_INFO_KEY,
        {
            "instagram": {
                "reason": "auth",
                "auth_state": "challenge",
                "since": clock.now().isoformat(),
            }
        },
    )
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={p: FakePlatformAdapter(p, clock=clock) for p in ("threads", "instagram")},
        media_dir=tmp_path / "media",
    )
    client = TestClient(create_app(state))
    home = client.get("/").text
    assert "인스타그램(본인 확인 창)" in home
    assert "인스타그램: Aside 브라우저에서 직접 본인 확인을 끝내면 풀림" in home
    inc = client.get("/incidents").text
    assert "본인 확인 창으로 자동 게시·답글 멈춤" in inc  # 받침에 맞는 조사
    assert "본인 확인 창" in inc and "직접 본인 확인을 끝내면 풀림" in inc
