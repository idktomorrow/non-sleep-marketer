from __future__ import annotations

import pytest

from nsm.domain.labels import AD_HASHTAG, BLOG_TITLE_SUFFIX, VIRTUAL_PERSON_TEXT
from nsm.domain.models import COMMENT_CATEGORIES
from nsm.domain.policy import apply_labels, decide_comment, labeled_blog_title
from nsm.ports import CAPABILITIES

ALL = set(CAPABILITIES)


def test_apply_labels_first_line() -> None:
    t = apply_labels("본문", is_ad=False)
    assert t.splitlines()[0] == VIRTUAL_PERSON_TEXT and t.endswith("본문")
    ad = apply_labels("본문", is_ad=True)
    assert ad.splitlines()[0] == f"{AD_HASHTAG} {VIRTUAL_PERSON_TEXT}"


def test_apply_labels_is_idempotent() -> None:
    once = apply_labels("본문", is_ad=True)
    assert apply_labels(once, is_ad=True) == once
    # 광고 표기를 끄면 #광고는 빠진다
    assert apply_labels(once, is_ad=False) == apply_labels("본문", is_ad=False)


def test_blog_title_suffix() -> None:
    assert labeled_blog_title("제목") == f"제목{BLOG_TITLE_SUFFIX}"
    assert labeled_blog_title(labeled_blog_title("제목")) == f"제목{BLOG_TITLE_SUFFIX}"


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("praise_general", "auto_reply"),
        ("question", "auto_reply"),
        ("complaint", "escalate"),
        ("price_refund", "escalate"),
        ("legal", "escalate"),
        ("spam", "hide"),
    ],
)
def test_six_categories(category: str, expected: str) -> None:
    assert decide_comment(category, 0, 3, ALL)[0] == expected


def test_all_categories_covered() -> None:
    for c in COMMENT_CATEGORIES:
        decide_comment(c, 0, 3, ALL)


def test_daily_reply_limit_per_author() -> None:
    assert decide_comment("question", 2, 3, ALL)[0] == "auto_reply"
    action, reason = decide_comment("question", 3, 3, ALL)
    assert action == "ignore" and "3번" in reason


def test_capability_fallbacks() -> None:
    no_hide = ALL - {"hide_comment"}
    action, reason = decide_comment("spam", 0, 3, no_hide)
    assert action == "escalate" and "스팸 추정" in reason
    assert decide_comment("praise_general", 0, 3, ALL - {"reply"})[0] == "escalate"


def test_apply_labels_strips_variants() -> None:
    messy = f"{AD_HASHTAG}\n{VIRTUAL_PERSON_TEXT}\n\n{AD_HASHTAG}\n본문"
    assert apply_labels(messy, is_ad=True) == apply_labels("본문", is_ad=True)


def test_apply_labels_strips_legacy_wording() -> None:
    from nsm.domain.labels import LEGACY_VIRTUAL_PERSON_TEXTS

    pasted = f"{LEGACY_VIRTUAL_PERSON_TEXTS[0]}\n\n본문"  # 예전에 게시한 글을 붙여 넣은 경우
    assert apply_labels(pasted, is_ad=False) == apply_labels("본문", is_ad=False)
