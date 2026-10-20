"""프리셋 전환(C29): 현재 프리셋 자동 저장 → 설정 교체 → DB 교체 → 복구."""

from __future__ import annotations

from pathlib import Path

from nsm.config.presets import PresetStore
from nsm.config.schema import AppConfig
from nsm.engine.context import JobContext
from nsm.runtime import switch_preset
from nsm.store.db import Database
from nsm.store.repo import Repository
from tests.fakes import FakeNotifier, FixedClock


def test_switch_preset_saves_current_and_swaps_db(
    app_config: AppConfig, tmp_path: Path, monkeypatch
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setenv("NSM_HOME", str(tmp_path))
    db = Database(tmp_path / "data.duckdb")
    clock = FixedClock(datetime(2026, 10, 21, 1, 0, tzinfo=UTC))
    repo = Repository(db, clock)
    notifier = FakeNotifier()
    ctx = JobContext(config=app_config, repo=repo, clock=clock, notifier=notifier)
    repo.set_setting("marker", "기본의 값")
    store = PresetStore()
    cafe = app_config.model_copy(
        update={
            "persona": app_config.persona.model_copy(update={"name": "루나", "topics": ["커피"]})
        }
    )
    store.save(cafe, "카페")
    saved: list[AppConfig] = []

    def save(cfg: AppConfig) -> None:
        saved.append(cfg)
        ctx.config = cfg

    assert switch_preset(ctx, db, "카페", save) == "카페"
    assert ctx.config.settings.preset == "카페" and ctx.config.persona.name == "루나"
    assert db.path == str(store.db_path("카페"))
    assert repo.get_setting("marker") is None  # 새 작업 공간은 비어 있다
    assert store.exists("기본")  # 떠나기 전 지금 설정을 자동 저장
    assert any("프리셋 전환" in n[0] for n in notifier.calls)
    # 다시 기본으로: 원래 데이터가 그대로
    switch_preset(ctx, db, "기본", save)
    assert repo.get_setting("marker") == "기본의 값" and ctx.config.settings.preset == "기본"
    assert switch_preset(ctx, db, "기본", save) == "기본"  # 같은 프리셋이면 아무 일 없음
    assert len(saved) == 2
    # 켤 때는 설정이 가리키는 프리셋의 DB를 연다(재시작 뒤에도 이어지게)
    from nsm.config.presets import workspace_db_path

    assert workspace_db_path(ctx.config) == store.db_path("기본")
    assert workspace_db_path(store.load_into(ctx.config, "카페")) == store.db_path("카페")
    # DB 교체가 실패하면 설정도 원래대로
    store.save(cafe, "깨짐")
    bad = store.db_path("깨짐")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"broken")
    import pytest

    with pytest.raises(Exception):  # noqa: B017
        switch_preset(ctx, db, "깨짐", save)
    assert ctx.config.settings.preset == "기본" and db.path == str(store.db_path("기본"))
    db.close()
