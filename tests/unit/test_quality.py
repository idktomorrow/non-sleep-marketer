"""품질 규칙(배치 Q1): 2026-10-12 드라이런에서 실제로 나온 문장으로 검사한다."""

from __future__ import annotations

import pytest

from nsm.domain import quality as Q
from tests.fakes import load_deck


# ---------------------------------------------------------------- 2. 제외 장르
@pytest.mark.parametrize(
    "title",
    [
        "후쿠오카 야쿠인 카페 커피카운티 핸드드립 스페셜티 커피 내돈내산 후기 ♥",  # 실제 수집 글
        "성수 카페 투어 다녀왔어요",
        "제주 여행기 3일차",
        "오늘의 일기",
        "드립백 솔직 후기",
    ],
)
def test_reviews_travelogues_diaries_are_excluded(title: str) -> None:
    assert Q.excluded_genre(title, "")


@pytest.mark.parametrize(
    "title",
    [
        "가정용 전동 커피 그라인더 원두 분쇄기 고르는 기준 5가지",  # 실제 수집 글(정보성)
        "핸드드립 물 온도, 왜 92도일까",
        "여행용 드립 키트 고르는 법",
        "일상에서 원두 보관하는 법",
    ],
)
def test_informative_posts_pass(title: str) -> None:
    assert Q.excluded_genre(title, "") is None


# ---------------------------------------------------------------- 6. 이중 번호
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("1. 분쇄 방식", "분쇄 방식"),
        ("2) 분쇄 단계", "분쇄 단계"),
        ("③ 휴대성", "휴대성"),
        ("- 신선도", "신선도"),
        ("✔ 보관법", "보관법"),
        ("92도가 기준", "92도가 기준"),
        ("10", "10"),
    ],
)
def test_strip_bullet_marker(raw: str, clean: str) -> None:
    assert Q.strip_bullet_marker(raw) == clean


def test_normalize_bullets_on_all_slide_kinds(app_config) -> None:
    from nsm.domain.models import ContentBrief
    from tests.fakes import FakeTextGenerator, load_source_items

    for template in ("info", "list", "compare"):
        gen = FakeTextGenerator().generate_content(
            ContentBrief(topic="t", template=template, sources=load_source_items()[:1]),
            app_config.persona,
            app_config.brand,
        )
        slides = []
        for s in gen.deck.slides:
            if s.kind in ("body", "list") and s.bullets:
                s = s.model_copy(
                    update={"bullets": [f"{i}. {b}" for i, b in enumerate(s.bullets, 1)]}
                )
            if s.kind == "compare":
                s = s.model_copy(
                    update={
                        "left": s.left.model_copy(
                            update={"points": [f"- {p}" for p in s.left.points]}
                        )
                    }
                )
            slides.append(s)
        dirty = gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})
        clean = Q.normalize_bullets(dirty)
        assert clean.deck == gen.deck  # 표시만 떼고 나머지는 그대로


# ---------------------------------------------------------------- 4·7. 페르소나 규칙
@pytest.mark.parametrize(
    ("tone", "limit"),
    [
        ("다정하고 짧은 존댓말, 이모지는 한 개까지", 1),
        ("이모지 2개 이하", 2),
        ("담백하게, 이모지 없이", 0),
        ("이모지는 쓰지 않는다", 0),
        ("다정한 존댓말", None),
    ],
)
def test_emoji_limit_from_tone(tone: str, limit: int | None) -> None:
    assert Q.emoji_limit(tone) == limit


def test_count_emoji_counts_symbols_seen_as_emoji() -> None:
    # 2026-10-12 캡션: ✔ 두 개 + ☕ 한 개 = 3
    caption = "✔ 첫 번째 기준\n✔ 그다음은\n시작돼요 ☕"
    assert Q.count_emoji(caption) == 3
    assert Q.count_emoji("커피 92도, 3가지(A·B) — 끝!") == 0


def _gen(app_config, caption: str = "본문", threads: str = "본문"):
    from nsm.domain.models import ContentBrief
    from tests.fakes import FakeTextGenerator, load_source_items

    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="t", template="info", sources=load_source_items()[:1]),
        app_config.persona,
        app_config.brand,
    )
    return gen.model_copy(update={"instagram_caption": caption, "threads_text": threads})


def test_rule_violations_found_in_real_dryrun_text(app_config) -> None:
    gen = _gen(
        app_config,
        caption="✔ 첫 번째 기준은 분쇄 방식이에요\n✔ 그다음은 휴대성\n쉬운 홈카페 ☕",
        threads="자료에 따르면 분쇄 방식, 분쇄 단계, 휴대성에 따라 쓰임새가 달라져요.",
    )
    v = Q.rule_violations(gen, app_config.persona)  # 픽스처 말투: 이모지는 한 개까지
    assert any("인스타그램 캡션 이모지 3개(상한 1개)" in x for x in v)
    assert any("자료에 따르면" in x for x in v)


def test_rule_violations_pass_for_clean_text(app_config) -> None:
    assert (
        Q.rule_violations(_gen(app_config, "깔끔한 캡션 ☕", "짧은 글이에요"), app_config.persona)
        == []
    )


# ---------------------------------------------------------------- 3. 출처 줄
@pytest.mark.parametrize(
    ("src", "label"),
    [
        ("https://blog.naver.com/7678ymym/224432096857", "네이버 블로그"),
        ("https://n.news.naver.com/mnews/article/001/0000001", "네이버 뉴스"),
        ("https://www.coffeeschool.kr/guide/1", "coffeeschool.kr"),
        ("브랜드 제공 자료", "브랜드 제공 자료"),
    ],
)
def test_site_label(src: str, label: str) -> None:
    assert Q.site_label(src) == label


def test_sources_line_dedupes_and_caps() -> None:
    srcs = [
        "https://blog.naver.com/a/1",
        "https://blog.naver.com/b/2",
        "https://n.news.naver.com/x/article/1",
        "https://example.com/z",
    ]
    assert Q.sources_line(srcs) == "출처: 네이버 블로그, 네이버 뉴스"
    assert Q.sources_line([]) == ""


def test_fixture_deck_is_loadable() -> None:
    assert load_deck("info").slides
