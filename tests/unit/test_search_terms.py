"""검색어 확장(관심 주제 + 별칭). 관심 분야 묶음 버튼은 2026-10-21에 뺐다(관심 주제 칸 하나로)."""

from __future__ import annotations

from nsm.domain.picks_policy import search_terms


def test_search_terms_expand_aliases_but_keep_canonical_first() -> None:
    terms = search_terms(
        ["치이카와 굿즈", "핸드드립"], {"치이카와 굿즈": ["ちいかわ グッズ", "치이카와 굿즈"]}
    )
    assert terms == ["치이카와 굿즈", "ちいかわ グッズ", "핸드드립"]
