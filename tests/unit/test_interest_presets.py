"""관심 분야 프리셋(S11): 검색어·별칭·제외어가 채워지고 추천 모드가 켜진다."""

from __future__ import annotations

import pytest

from nsm.domain.interest_presets import PRESETS, apply_preset, search_terms


def test_presets_cover_four_fields() -> None:
    assert set(PRESETS) == {"chiikawa", "lol", "celeb", "fashion"}
    for p in PRESETS.values():
        assert p.topics and all(k in p.topics for k in p.aliases)


def test_apply_preset_sets_topics_aliases_exclude_and_enables_picks(app_config) -> None:
    new = apply_preset(app_config, "lol")
    assert new.persona.topics == PRESETS["lol"].topics
    assert new.settings.picks.enabled and new.settings.picks.aliases == PRESETS["lol"].aliases
    assert "김밥" in new.settings.collect.exclude_words
    assert app_config.settings.picks.enabled is False  # 원본은 그대로
    with pytest.raises(KeyError):
        apply_preset(app_config, "nope")


def test_search_terms_expand_aliases_but_keep_canonical_first() -> None:
    terms = search_terms(
        ["치이카와 굿즈", "핸드드립"], {"치이카와 굿즈": ["ちいかわ グッズ", "치이카와 굿즈"]}
    )
    assert terms == ["치이카와 굿즈", "ちいかわ グッズ", "핸드드립"]
