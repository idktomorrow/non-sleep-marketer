"""프리셋(C29, ADR 0049): 저장·불러오기·목록·삭제, 캐릭터 그림 복사, DB 경로, DB 갈아끼우기."""

from __future__ import annotations

from pathlib import Path

import pytest

from nsm.config.presets import DEFAULT_PRESET, PresetError, PresetStore, slugify
from nsm.config.schema import AppConfig
from nsm.store.db import Database


def test_slugify_keeps_korean_and_strips_path_chars() -> None:
    assert slugify("치이카와 굿즈") == "치이카와-굿즈"
    assert slugify(" a/b:c ") == "a-b-c"
    with pytest.raises(PresetError):
        slugify("  ")


def test_save_load_roundtrip_keeps_handle_and_copies_images(
    app_config: AppConfig, tmp_path: Path
) -> None:
    store = PresetStore(tmp_path / "presets", tmp_path / "data.duckdb")
    img = tmp_path / "cover.png"
    img.write_bytes(b"png")
    cfg = app_config.model_copy(
        update={
            "brand": app_config.brand.model_copy(
                update={"character": "custom", "character_cover": str(img), "name": "브랜드B"}
            ),
            "persona": app_config.persona.model_copy(
                update={"name": "루나", "topics": ["커피", "원두"]}
            ),
        }
    )
    store.save(cfg, "카페")
    assert store.exists("카페") and not store.exists("없음")
    # 그림은 프리셋 폴더로 복사되고 원본 경로 대신 복사본을 가리킨다
    copied = store.dir("카페") / "brand" / "character_cover.png"
    assert copied.read_bytes() == b"png"
    other = app_config.model_copy(
        update={"persona": app_config.persona.model_copy(update={"handle": "@acct"})}
    )
    loaded = store.load_into(other, "카페")
    assert loaded.persona.name == "루나" and loaded.persona.topics == ["커피", "원두"]
    assert loaded.persona.handle == "@acct"  # 계정은 프리셋과 무관
    assert loaded.brand.name == "브랜드B" and loaded.brand.character_cover == str(copied)
    assert loaded.settings.preset == "카페"
    infos = store.list()
    assert [i.name for i in infos] == ["카페"] and infos[0].topics == ("커피", "원두")
    # 기본 프리셋은 기존 DB, 나머지는 프리셋 폴더 안
    assert store.db_path(DEFAULT_PRESET) == tmp_path / "data.duckdb"
    assert store.db_path("카페") == store.dir("카페") / "data.duckdb"


def test_name_rules_and_delete_guards(app_config: AppConfig, tmp_path: Path) -> None:
    store = PresetStore(tmp_path / "presets", tmp_path / "data.duckdb")
    with pytest.raises(PresetError):
        store.save(app_config, "a/b")
    with pytest.raises(PresetError):
        store.save(app_config, "")
    store.save(app_config, "하나")
    store.save(app_config, "둘")
    with pytest.raises(PresetError):
        store.delete("하나", current="하나")  # 쓰는 중
    with pytest.raises(PresetError):
        store.delete(DEFAULT_PRESET, current="하나")
    with pytest.raises(PresetError):
        store.load_into(app_config, "셋")
    store.delete("둘", current="하나")
    assert [i.name for i in store.list()] == ["하나"]
    # 같은 폴더가 되는 다른 이름: 지우기 보호는 폴더 기준, 저장은 거부
    with pytest.raises(PresetError):
        store.delete("하-나", current="하 나")  # "하 나"와 "하-나"는 같은 폴더
    store.save(app_config, "내 브랜드")
    with pytest.raises(PresetError, match="같은 폴더"):
        store.save(app_config, "내-브랜드")
    with pytest.raises(PresetError, match="이미 있습니다"):
        store.save(app_config, "내 브랜드", overwrite=False)
    assert store.db_path("기본") == store.default_db and store.db_path("기본 ") == store.default_db
    assert store.exists("") is False


def test_database_switch_failure_keeps_old_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.duckdb")
    db.execute("INSERT INTO settings_kv VALUES ('k', 'a')")
    bad = tmp_path / "bad.duckdb"
    bad.write_bytes(b"not a duckdb file")
    with pytest.raises(Exception):  # noqa: B017  깨진 파일은 duckdb가 거부한다
        db.switch(bad)
    assert db.path.endswith("a.duckdb")
    assert db.execute("SELECT value FROM settings_kv WHERE key = 'k'") == [("a",)]
    db.close()


def test_database_switch_keeps_object_and_isolates_data(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.duckdb")
    db.execute("INSERT INTO settings_kv VALUES ('k', 'a')")
    db.switch(tmp_path / "sub" / "b.duckdb")  # 폴더도 만든다, 새 파일엔 스키마가 깔린다
    assert db.path.endswith("b.duckdb") and db.schema_version >= 1
    assert db.execute("SELECT count(*) FROM settings_kv") == [(0,)]
    db.execute("INSERT INTO settings_kv VALUES ('k', 'b')")
    db.switch(tmp_path / "a.duckdb")
    assert db.execute("SELECT value FROM settings_kv WHERE key = 'k'") == [("a",)]
    db.switch(tmp_path / "a.duckdb")  # 같은 파일이면 아무 일 없음
    assert db.execute("SELECT count(*) FROM settings_kv") == [(1,)]
    db.close()
