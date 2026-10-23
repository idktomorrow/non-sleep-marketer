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


def test_fixture_deck_is_loadable() -> None:
    assert load_deck("info").slides


# ---------------------------------------------------------------- 코드 리뷰 반영
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("3.5도 차이", "3.5도 차이"),
        ("1.5배 진하게", "1.5배 진하게"),
        ("*주의*", "*주의*"),
        ("92.5도에서 추출", "92.5도에서 추출"),
        ("1) 물 온도", "물 온도"),
    ],
)
def test_numbers_and_emphasis_are_not_markers(raw: str, clean: str) -> None:
    assert Q.strip_bullet_marker(raw) == clean


@pytest.mark.parametrize(
    "title",
    [
        "매일기록하는 홈카페 레시피",
        "내일 일기예보와 커피",
        "월요일기분 전환 라떼",
        "커피 산지 다녀온 바리스타의 원두 고르는 법",
        "핸드드립 웨이팅 시간",
    ],
)
def test_genre_rule_does_not_catch_informative_titles(title: str) -> None:
    assert Q.excluded_genre(title, "") is None


@pytest.mark.parametrize("title", ["홈카페 일기 3일차", "성수 카페 리뷰", "웨이팅 길었던 카페"])
def test_genre_rule_still_catches_diaries_and_reviews(title: str) -> None:
    assert Q.excluded_genre(title, "")


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("👍🏻", 1),
        ("🇰🇷", 1),
        ("👨\u200d👩\u200d👧", 1),
        ("1️⃣ 2️⃣", 2),
        ("☕️", 1),
        ("좋아요 👍🏻 ☕", 2),
    ],
)
def test_count_emoji_counts_what_people_see(text: str, n: int) -> None:
    assert Q.count_emoji(text) == n


@pytest.mark.parametrize(
    ("tone", "limit"),
    [
        ("이모지는 최대 한 개", 1),
        ("이모지 1~2개", 2),
        ("이모지는 한 개 정도", 1),
        ("이모지는 두 개만", 2),
    ],
)
def test_emoji_limit_more_phrasings(tone: str, limit: int) -> None:
    assert Q.emoji_limit(tone) == limit


def test_unconfirmed_rumors_are_violations(app_config) -> None:
    # 2026-10-13 실제 원고: 개봉 전 블로그 글을 보고 "가격은 아직 소문 단계"라고 썼다
    gen = _gen(
        app_config,
        caption="가격은 아직 소문 단계예요.\n커뮤니티에서는 29,000원 선이라는 이야기가 돌아요.",
        threads="공식 세부 금액은 아직 공지 전이에요.",
    )
    v = Q.rule_violations(gen, app_config.persona)
    assert any("인스타그램 캡션에 확인되지 않은 내용" in x for x in v)
    assert any("스레드 글에 확인되지 않은 내용" in x for x in v)
    ok = _gen(app_config, "정가는 2만9000원이에요(10월 13일 기사 기준)", "11월 출시 예정이래요")
    assert Q.rule_violations(ok, app_config.persona) == []


def _with_cta(gen, cta: str):
    slides = [
        s.model_copy(update={"cta": cta}) if s.kind == "outro" else s for s in gen.deck.slides
    ]
    return gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})


@pytest.mark.parametrize(
    "cta", ["링크는 저장부터 해요", "출처 확인하고 저장해요", "프로필 링크 클릭!"]
)
def test_awkward_outro_cta_is_a_violation(app_config, cta: str) -> None:
    gen = _with_cta(_gen(app_config), cta)
    assert any("마무리 문구" in v for v in Q.rule_violations(gen, app_config.persona))


@pytest.mark.parametrize(
    "cta", ["여러분은 보셨나요?", "어떤 캐릭터가 제일 좋아요?", "저장해 두고 다시 봐요"]
)
def test_natural_outro_cta_passes(app_config, cta: str) -> None:
    gen = _with_cta(_gen(app_config), cta)
    assert not any("마무리 문구" in v for v in Q.rule_violations(gen, app_config.persona))


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://www.yna.co.kr/view/AKR20261012042600005?input=1195m", "연합뉴스"),
        ("https://www.ize.co.kr/news/articleView.html?idxno=81538", "아이즈"),
        ("https://ent.sbs.co.kr/news/article.do?article_id=E1", "SBS"),
        ("https://news.jtbc.co.kr/article/NB1", "JTBC"),
        ("http://www.finomy.com/news/articleView.html?idxno=263459", "finomy.com"),
        ("브랜드 제공 자료", "브랜드 제공 자료"),
    ],
)
def test_site_label_knows_news_sites(url: str, name: str) -> None:
    assert Q.site_label(url) == name


def test_card_sources_are_short_names_once() -> None:
    srcs = [
        "https://www.yna.co.kr/view/A",
        "https://www.yna.co.kr/view/B",
        "https://blog.naver.com/qmfosej/224433414252",
    ]
    assert Q.card_sources(srcs) == ["연합뉴스", "네이버 블로그"]
