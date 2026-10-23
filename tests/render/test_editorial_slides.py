"""경계 3: 소식형(editorial 템플릿)의 상품 그리드·일정 패널 장 (S12 제안, ADR 0052).

덱 JSON 계약 변경이 들어간 제안이다. 네 스타일 모두에서 1080×1350·넘침 없음, 표기 슬롯 동일, 글자는 HTML.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nsm.domain.models import CardDeck
from nsm.render.html_renderer import HtmlCardRenderer, build_html
from tests.render.test_card_styles import STYLES
from tests.render.test_html_renderer import FIXTURES, _cover_sizes, png_size, slots

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "nsm" / "templates" / "samples" / "sample-photo.jpg"


def deck(
    n_items: int = 4, with_bg: bool = True, long: bool = False, item_image: str | None = None
) -> CardDeck:
    raw = json.loads((FIXTURES / "editorial.json").read_text("utf-8"))
    for s in raw["slides"]:
        s["background"] = str(SAMPLE) if (with_bg and s["background"]) else None
    products = next(s for s in raw["slides"] if s["kind"] == "products")
    products["items"] = products["items"][:n_items]
    for it in products["items"]:
        it["image"] = item_image
        if long:
            it["name"] = "아주 길어서 두 줄 세 줄로 접히는 한정판 콜라보 마스코트 볼체인 인형 세트"
            it["note"] = "전 12종 랜덤 · 온라인 한정 · 1인 2개까지 구매 가능"
    if long:
        facts = next(s for s in raw["slides"] if s["kind"] == "facts")
        facts["rows"] = [
            {
                "label": "아주 긴 항목 이름",
                "value": "확인 가능한 정보라도 길게 들어오면 줄어들어 들어가야 합니다 " * 2,
            }
        ] * 6
    return CardDeck.model_validate(raw)


@pytest.fixture(scope="module")
def renderers():
    rs = {s: HtmlCardRenderer(style=s) for s in STYLES}
    yield rs
    for r in rs.values():
        r.close()


def test_contract_shape_and_limits() -> None:
    d = deck()
    assert [s.kind for s in d.slides] == ["cover", "body", "products", "facts", "outro"]
    raw = json.loads((FIXTURES / "editorial.json").read_text("utf-8"))
    bad = json.loads(json.dumps(raw))
    bad["slides"][2]["items"] = bad["slides"][2]["items"][:1]  # 상품 1개는 그리드가 아니다
    with pytest.raises(ValidationError):
        CardDeck.model_validate(bad)
    bad = json.loads(json.dumps(raw))
    bad["slides"][2]["items"] = bad["slides"][2]["items"] * 2  # 5개 이상 금지
    with pytest.raises(ValidationError):
        CardDeck.model_validate(bad)
    bad = json.loads(json.dumps(raw))
    bad["template"] = "info"  # 다른 템플릿에는 products·facts를 섞을 수 없다
    with pytest.raises(ValidationError):
        CardDeck.model_validate(bad)


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("n_items", [2, 3, 4])
def test_renders_in_every_style_without_overflow(
    renderers, style: str, n_items: int, tmp_path: Path
) -> None:
    for name, d in (("normal", deck(n_items)), ("long", deck(n_items, long=True))):
        paths = renderers[style].render(d, tmp_path / name)
        assert len(paths) == 5 and all(png_size(p) == (1080, 1350) for p in paths)
        assert renderers[style].last_overflow == [], (
            f"{style} {n_items} {name}: {renderers[style].last_overflow}"
        )


def test_label_slots_text_and_images() -> None:
    d = deck(item_image=str(SAMPLE))
    s = slots(d)
    # 이름·가상인물 배지·쪽 번호는 카드에 없다(S18). 출처는 마무리 장에
    assert all("virtual-person" not in x["labels"] and x["page"] == 0 for x in s)
    assert [x["sources"] for x in s] == [0, 0, 0, 0, 1]
    html = build_html(d, "editorial")
    for it in d.slides[2].items:  # 이름·가격은 HTML 글자
        assert it.name in html and it.price in html
    for r in d.slides[3].rows:
        assert r.label in html and r.value in html
    assert html.count('class="product-img"') == 4 and SAMPLE.resolve().as_uri() in html
    # 그림이 없으면 자리표시 상자(이름만 크게), 없는 경로는 조용히 자리표시로
    assert build_html(deck(), "editorial").count("product-img placeholder") == 4
    assert (
        build_html(deck(item_image="nope/x.jpg"), "editorial").count("product-img placeholder") == 4
    )


def test_cover_still_readable_at_thumbnail(tmp_path: Path) -> None:
    sizes = _cover_sizes(deck(), tmp_path, "editorial")
    assert sizes["title"] * (224 / 1080) >= 18 and sizes["subtitle"] * (224 / 1080) >= 9.9
