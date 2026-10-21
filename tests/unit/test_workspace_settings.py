"""작업 공간별 설정(S13 제안): settings.picks의 count·aliases를 workspace로 옮기고 프리셋 파일에 함께 저장한다.

- 예전 설정(workspace 절 없음)은 picks.count/aliases를 가져온다
- workspace 절이 있으면 picks 값은 무시한다: aliases={}는 사용자가 비운 값이지 "없음"이 아니다
- 프리셋 저장·복원이 workspace를 함께 다루고, 예전 프리셋 파일은 기본값으로 시작한다
"""

from __future__ import annotations

from pathlib import Path

from nsm.config.presets import PresetStore
from nsm.config.schema import AppConfig


def _base(app_config: AppConfig) -> dict:
    d = app_config.model_dump(mode="json")
    d.pop("workspace", None)
    return d


def test_legacy_picks_fields_move_into_workspace(app_config) -> None:
    raw = _base(app_config)
    raw["settings"]["picks"] = {"enabled": True, "count": 2, "aliases": {"치이카와": ["ちいかわ"]}}
    cfg = AppConfig.model_validate(raw)
    assert cfg.workspace.info_count == 2 and cfg.workspace.aliases == {"치이카와": ["ちいかわ"]}
    assert cfg.daily_slot_total == 2


def test_explicit_empty_workspace_values_are_kept(app_config) -> None:
    raw = _base(app_config)
    raw["settings"]["picks"] = {"enabled": True, "count": 2, "aliases": {"치이카와": ["ちいかわ"]}}
    raw["workspace"] = {"aliases": {}, "info_count": 1, "meme_count": 1}
    cfg = AppConfig.model_validate(raw)
    assert cfg.workspace.aliases == {} and cfg.workspace.info_count == 1
    assert cfg.workspace.meme_count == 1 and cfg.workspace.schema_version == 1


def test_defaults_without_any_picks_fields(app_config) -> None:
    raw = _base(app_config)
    raw["settings"].pop("picks", None)
    cfg = AppConfig.model_validate(raw)
    assert cfg.workspace.info_count == 3 and cfg.workspace.meme_count == 0
    assert cfg.workspace.production_mode == "plan_first"  # 유료 제작은 켜지 않은 채 시작


def test_preset_round_trips_workspace(app_config, tmp_path: Path) -> None:
    store = PresetStore(root=tmp_path / "presets", default_db=tmp_path / "data.duckdb")
    cfg = app_config.model_copy(
        update={
            "workspace": app_config.workspace.model_copy(
                update={"aliases": {"롤": ["LoL"]}, "info_count": 1, "meme_count": 2}
            )
        }
    )
    store.save(cfg, "롤판")
    other = store.load_into(app_config, "롤판")
    assert other.workspace.aliases == {"롤": ["LoL"]}
    assert (other.workspace.info_count, other.workspace.meme_count) == (1, 2)
    # 예전 프리셋 파일(workspace 절 없음)은 지금 설정을 끌고 가지 않고 기본값으로 시작한다
    f = store.dir("롤판") / "preset.yaml"
    f.write_text(f.read_text(encoding="utf-8").split("workspace:")[0], encoding="utf-8")
    assert store.load_into(cfg, "롤판").workspace.aliases == {}
