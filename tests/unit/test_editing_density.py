"""편집 밀도(S18 제안, ADR 0063 보충): 원고를 쓰기 전에 실제 사진을 관찰하고, 원고가 관점·장·확대·주석을 고른다.
밈은 재미의 핵심을 그림 지시에 넘기고, 완성 그림을 검토해 문제 단계를 가른다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from nsm.adapters.generator.prompts import build_deck, content_from_output, llm_schema, user_prompt
from nsm.domain.models import ContentBrief, ObservedPhoto, PhotoDetail
from nsm.engine.photo_match import observe, split_photos
from tests.fakes import load_source_items


def _img(tmp_path: Path, name: str, size=(2400, 3000)) -> str:
    p = tmp_path / name
    Image.new("RGB", size, (230, 120, 160)).save(p)
    return str(p)


def _photos(tmp_path: Path) -> list[ObservedPhoto]:
    return [
        ObservedPhoto(
            path=_img(tmp_path, "front.jpg"),
            source_url="https://a/1",
            kind="photo",
            shows="파란 귀 인형 키링 앞면",
            details=[
                PhotoDetail(what="자수로 놓은 큰 눈", box=[30, 30, 40, 20]),
                PhotoDetail(what="풍성한 꼬리", box=[60, 70, 30, 25]),
            ],
        ),
        ObservedPhoto(
            path=_img(tmp_path, "thumb.jpg"), kind="designed", shows="블로그 제목 썸네일"
        ),
        ObservedPhoto(path=_img(tmp_path, "back.jpg"), kind="official", shows="같은 인형 뒷면"),
    ]


def _slide(**kw) -> dict:
    base = {
        "heading": "",
        "body": "",
        "bullets": [],
        "left_title": "",
        "left_points": [],
        "right_title": "",
        "right_points": [],
        "products": [],
        "facts": [],
        "facts_layout": "rows",
        "photo_hint": "",
        "photo": -1,
        "focus": -1,
        "marks": [],
    }
    base.update(kw)
    return base


def _out(slides: list[dict], **kw) -> dict:
    base = {
        "topic": "t",
        "cover_title": "괴물화 키링",
        "cover_subtitle": "",
        "slides": slides,
        "cta": "",
        "angle": "눈과 꼬리 디테일",
        "cover_photo": 0,
        "gaps": [],
        "instagram_caption": "c",
        "threads_text": "t",
        "claims": [],
    }
    base.update(kw)
    return base


def _brief(photos) -> ContentBrief:
    return ContentBrief(
        topic="t", template="editorial", sources=load_source_items()[:1], photos=photos
    )


def test_writer_sees_observed_photos_and_schema_asks_for_angle_and_focus(tmp_path) -> None:
    photos = _photos(tmp_path)
    text = user_prompt(_brief(photos))
    assert (
        '<photo n="0" kind="photo">파란 귀 인형 키링 앞면' in text
        and "[디테일 0] 자수로 놓은 큰 눈" in text
    )
    assert "독자가 무엇을 알게 되고" in text and "focus" in text
    # 기획에 사진의 물리 정보(크기·표지 배율·글자)를 넘긴다(S18 프로세스: 표지를 채울 수 있는 사진을 고르게)
    sized = [photos[0].model_copy(update={"size": [886, 432], "has_text": True})]
    assert 'size="886x432" cover_scale="3.1" text="yes"' in user_prompt(_brief(sized))
    s = llm_schema(False, "editorial")
    assert {"angle", "cover_photo", "gaps"} <= set(s["required"])
    assert {"photo", "focus", "marks"} <= set(s["properties"]["slides"]["items"]["required"])
    # 다른 템플릿은 칸 자체가 없다
    assert "angle" not in llm_schema(False, "info")["properties"]
    # 사진이 없으면 편집 지침·사진 목록을 붙이지 않는다(예전처럼 글만 보고 쓴다)
    assert "<photo n=" not in user_prompt(_brief([])) and "편집(관찰한 사진" not in user_prompt(
        _brief([])
    )


def test_deck_gets_cover_zoomed_detail_and_marks_only_from_observation(
    app_config, tmp_path
) -> None:
    photos = _photos(tmp_path)
    out = _out(
        [
            _slide(heading="뒷모습", body="꼬리부터 보세요", photo=2),
            _slide(
                heading="이 눈",
                body="자수 눈이 크게 들어가요",
                photo=0,
                focus=0,
                marks=[{"text": "자수 눈", "detail": 0}, {"text": "없는 디테일", "detail": 7}],
            ),
            _slide(heading="썸네일", body="블로그 썸네일", photo=1),  # designed: 쓰지 않는다
        ]
    )
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(photos))
    cover, whole, zoom, thumb = deck.slides[:4]
    assert cover.background == photos[0].path
    assert whole.photo.path == photos[2].path and whole.photo.crop is None
    assert zoom.photo.crop is not None and zoom.photo.crop[0] < 30 and zoom.photo.crop[2] > 40
    assert [m.text for m in zoom.photo.marks] == ["자수 눈"]  # 관찰에 없는 디테일은 짚지 않는다
    m = zoom.photo.marks[0]
    assert 30 < m.x < 70 and 20 < m.y < 80  # 확대한 영역 기준 좌표
    assert thumb.photo is None


def test_products_without_visible_products_shrink_and_report_gap(app_config, tmp_path) -> None:
    photos = _photos(tmp_path)
    products = [
        {"name": "마스코트", "price": "1,650엔", "note": "4종", "photo": -1},
        {"name": "인형", "price": "1,980엔", "note": "", "photo": 1},  # 썸네일은 상품 사진이 아니다
    ]
    out = _out([_slide(heading="2종", products=products)], angle="")
    gen = content_from_output(out, _brief(photos), app_config.brand, app_config.persona)
    kinds = [s.kind for s in gen.deck.slides]
    assert "products" not in kinds and "facts" in kinds
    assert any(n.startswith("자료 부족: 상품 사진 없음") for n in gen.editor_notes)
    # 상품이 보이는 사진이 있으면 그 사진으로 상품 장(같은 사진을 두 상품에 쓰지 않는다)
    products = [dict(products[0], photo=0), dict(products[1], photo=0)]
    deck = build_deck(
        "editorial", _out([_slide(heading="2종", products=products)]), app_config.brand,
        app_config.persona, _brief(photos),
    )  # fmt: skip
    items = next(s for s in deck.slides if s.kind == "products").items
    assert items[0].image == photos[0].path and items[1].image is None


def test_angle_and_gaps_become_editor_notes_not_card_text(app_config, tmp_path) -> None:
    out = _out([_slide(heading="앞모습", body="눈부터 보세요", photo=0)], gaps=["뒷면 사진 없음"])
    gen = content_from_output(out, _brief(_photos(tmp_path)), app_config.brand, app_config.persona)
    assert gen.editor_notes[:2] == ["관점: 눈과 꼬리 디테일", "자료 부족: 뒷면 사진 없음"]
    assert "관점" not in gen.instagram_caption


def test_observe_validates_boxes_and_split_keeps_photos_out_of_deck_until_confirmed(
    app_config, tmp_path
) -> None:
    rows = [{"path": _img(tmp_path, "a.jpg"), "source_url": "https://a"}]

    class Observer:
        def observe_photos(self, photos):
            assert photos[0][1] == "https://a"
            return [
                {
                    "photo": 0,
                    "kind": "photo",
                    "shows": "인형",
                    "details": [
                        {
                            "what": "눈",
                            "box": {"x": 90, "y": 10, "w": 30, "h": 20},
                        },  # 오른쪽을 넘침 → 자름
                        {"what": "점", "box": {"x": 5, "y": 5, "w": 1, "h": 1}},  # 너무 작음 → 버림
                    ],
                }
            ]

    obs = observe(SimpleNamespace(text_generator=Observer()), rows)
    assert obs[0].shows == "인형" and [d.box for d in obs[0].details] == [[90.0, 10.0, 10.0, 20.0]]
    assert (
        observe(SimpleNamespace(text_generator=object()), rows) == []
    )  # 관찰 공급자가 없으면 글만 보고 쓴다

    photos = _photos(tmp_path)
    out = _out(
        [
            _slide(
                heading="이 눈",
                body="자수 눈",
                photo=0,
                focus=0,
                marks=[{"text": "자수 눈", "detail": 0}],
            )
        ]
    )
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(photos))
    rows = [{"path": p.path} for p in photos]
    bare, placement = split_photos(deck, rows)
    assert bare.slides[0].background is None and bare.slides[1].photo is None
    assert placement["cover"] == 0 and placement["slides"] == {"1": 0}
    assert (
        placement["assets"]["1"]["crop"]
        and placement["assets"]["1"]["marks"][0]["text"] == "자수 눈"
    )


def test_zoom_and_marks_render_without_overflow(tmp_path) -> None:
    from nsm.domain.models import (
        BodySlide,
        CardDeck,
        CoverSlide,
        DeckBrand,
        DeckPersona,
        ImageAsset,
        PhotoMark,
    )
    from nsm.render.html_renderer import HtmlCardRenderer, build_html

    path = _img(tmp_path, "z.jpg", (1200, 900))
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#E05A8A", accent_color="#FFD34E"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="괴물화 키링", background=path),
            BodySlide(
                heading="이 눈",
                body="자수 눈이 크게 들어가요",
                photo=ImageAsset(
                    path=path, crop=[20, 20, 40, 40], marks=[PhotoMark(text="자수 눈", x=50, y=40)]
                ),
            ),
        ],
    )
    html = build_html(deck, "kitsch")
    assert 'class="zoom-box"' in html and "width: 250.0%" in html and "자수 눈" in html
    r = HtmlCardRenderer(style="kitsch")
    try:
        r.render(deck, tmp_path / "out")
        assert r.last_overflow == []
    finally:
        r.close()


@pytest.mark.parametrize(
    ("answer", "regenerates"),
    [
        ({"ok": False, "problems": [{"part": "composite", "note": "캐릭터가 떠 보임"}]}, True),
        ({"ok": False, "problems": [{"part": "text", "note": "자막이 얼굴을 가림"}]}, False),
        ({"ok": True, "problems": []}, False),
    ],
)
def test_finished_meme_review_names_the_failing_step(answer, regenerates, monkeypatch) -> None:
    from nsm.engine import production as P

    reasons = {"c1": "오늘의 추천; 완성 그림 검토: 예전 지적"}
    scenes, queued = {}, []
    slot = SimpleNamespace(target="s0.image", slide=0, scene="원본 짤의 남자를 캐릭터로")
    page = SimpleNamespace(overlays=[])
    content = SimpleNamespace(
        review_reason=reasons["c1"], generated=SimpleNamespace(deck=SimpleNamespace(slides=[page]))
    )

    class Reviewer:
        def review_meme_image(self, path, brief):
            assert "재미의 핵심(core): 시선이 신상으로" in brief
            return answer

    ctx = SimpleNamespace(
        text_generator=Reviewer(),
        repo=SimpleNamespace(
            db=None,
            get_content=lambda cid: SimpleNamespace(
                review_reason=reasons[cid], generated=content.generated
            ),
            set_review_reason=lambda cid, r: reasons.__setitem__(cid, r),
        ),
        clock=None,
    )
    prod = SimpleNamespace(
        images=lambda cid, target: [1],
        set_scene=lambda cid, target, scene: scenes.__setitem__(target, scene),
    )
    monkeypatch.setattr(P, "current_slots", lambda *a, **k: (None, None, [slot]))
    monkeypatch.setattr(P, "enqueue_production", lambda *a, **k: queued.append(k))

    class Slots:
        def __init__(self, *a):
            pass

        def by_content(self, cid):
            return SimpleNamespace(plan={"core": "시선이 신상으로", "make": "composite"})

    monkeypatch.setattr("nsm.store.slots.SlotStore", Slots)
    job = SimpleNamespace(content_id="c1", target="s0.image", result_path="x.png")
    P.review_meme_image(ctx, prod, job)
    tags = [x for x in reasons["c1"].split("; ") if x.startswith(P.MEME_REVIEW_TAG)]
    if answer["ok"]:
        assert tags == []  # 예전 지적도 지운다
    else:
        assert len(tags) == 1 and answer["problems"][0]["note"] in tags[0]
    assert bool(queued) == regenerates and ("고칠 점" in scenes.get("s0.image", "")) == regenerates


def test_cover_photo_is_not_repeated_whole_and_empty_compare_heading_is_specific(
    app_config, tmp_path
) -> None:
    photos = _photos(tmp_path)
    out = _out(
        [
            _slide(heading="앞모습", body="b", photo=0),  # 표지와 같은 사진 통째로: 빼기
            _slide(heading="눈", body="자수 눈", photo=0, focus=0),  # 확대는 된다
            _slide(left_title="오전", left_points=["a"], right_title="오후", right_points=["b"]),
        ]
    )
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(photos))
    assert deck.slides[1].photo is None and deck.slides[2].photo.crop
    assert deck.slides[3].heading == "오전 · 오후"


def test_too_few_usable_photos_pull_photos_from_related_posts(tmp_path) -> None:
    from nsm.engine import picks as P

    lead = SimpleNamespace(url="https://a/1", title="치이카와 ANA 굿즈 구매 제한")
    related = SimpleNamespace(url="https://b/2", title="치이카와 ANA 굿즈 마스코트 사진")
    other = SimpleNamespace(url="https://c/3", title="극장판 4DX 확정")

    def fetch(url, dest):
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "cand-00.jpg"
        Image.new("RGB", (800, 800)).save(p)
        return [
            SimpleNamespace(path=p, image_url=url + "/i.jpg", source_url=url, width=800, height=800)
        ]

    class Observer:
        def observe_photos(self, photos):
            return [
                {"photo": k, "kind": "photo", "shows": s, "details": []}
                for k, (_p, s, _a) in enumerate(photos)
            ]

    ctx = SimpleNamespace(
        media_dir=tmp_path,
        text_generator=Observer(),
        config=SimpleNamespace(
            workspace=SimpleNamespace(aliases={}), settings=SimpleNamespace(relevance_threshold=0.5)
        ),
        repo=SimpleNamespace(
            unused_relevant_items=lambda th: [(related, 0.9, None), (other, 0.9, None)]
        ),
    )
    rows, observed = P.gather_photos(ctx, "c1", [lead], fetch)
    assert [r["source_url"] for r in rows] == [
        "https://a/1",
        "https://b/2",
    ]  # 관련 없는 글은 안 받는다
    assert [p.shows for p in observed] == ["https://a/1", "https://b/2"]
    assert rows[1]["observed"]["kind"] == "photo"  # 무엇을 보고 골랐는지 후보 목록에 남는다
    # 다시 편집: 이미 받은 파일은 두 번 넣지 않는다
    again, _ = P.gather_photos(ctx, "c1", [lead], fetch, rows=[dict(r) for r in rows])
    assert len(again) == 2


def test_zoom_and_marks_must_talk_about_the_observed_detail(app_config, tmp_path) -> None:
    """확대한 디테일과 다른 특징을 쓰면 확대·주석을 버린다(실호출: 귀를 확대하고 '소매 장식'이라고 씀)."""
    photos = _photos(tmp_path)
    out = _out(
        [
            _slide(
                heading="소매 장식",
                body="소매와 가슴 장식을 보세요",
                photo=0,
                focus=1,  # 관찰: 풍성한 꼬리
                marks=[{"text": "가슴 장식", "detail": 1}, {"text": "꼬리", "detail": 1}],
            )
        ],
        cover_photo=2,  # 표지와 같은 사진을 통째로 쓰면 빠지므로 다른 표지
    )
    deck = build_deck("editorial", out, app_config.brand, app_config.persona, _brief(photos))
    photo = deck.slides[1].photo
    assert photo.crop is None and [m.text for m in photo.marks] == ["꼬리"]


def test_zoom_on_a_small_photo_is_skipped_for_sharpness(app_config, tmp_path) -> None:
    """화질(S18): 잘라 낸 영역을 칸 폭으로 1.6배 넘게 늘려야 하면 확대하지 않고 전체 사진으로, 이유는 자료 부족에."""
    small = ObservedPhoto(
        path=_img(tmp_path, "small.jpg", (800, 390)),
        kind="official",
        shows="인형",
        details=[PhotoDetail(what="금색 단추", box=[10, 10, 30, 40])],
    )
    out = _out([_slide(heading="금색 단추", body="금색 단추", photo=0, focus=0)], cover_photo=-1)
    gaps: list[str] = []
    cover = ObservedPhoto(
        path=_img(tmp_path, "c.jpg", (1200, 1500)), kind="photo", size=[1200, 1500]
    )
    deck = build_deck(
        "editorial", out, app_config.brand, app_config.persona, _brief([small, cover]), gaps
    )
    assert deck.slides[1].photo is not None and deck.slides[1].photo.crop is None
    assert any("해상도 부족" in g for g in gaps)


def test_wide_group_product_photos_are_stacked_uncropped(tmp_path) -> None:
    """잘림(S18 실호출: 2:1 단체 사진을 거의 정사각 칸에 cover로 넣어 양 끝 상품이 잘림): 칸이 사진 비율을 따르고 한 줄에 하나씩."""
    from nsm.domain.models import (
        CardDeck,
        CoverSlide,
        DeckBrand,
        DeckPersona,
        ProductItem,
        ProductsSlide,
    )
    from nsm.render.html_renderer import HtmlCardRenderer, build_html

    a, b = _img(tmp_path, "a.jpg", (886, 432)), _img(tmp_path, "b.jpg", (886, 432))
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#E05A8A", accent_color="#FFD34E"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[
            CoverSlide(title="ANA 굿즈"),
            ProductsSlide(
                heading="마스코트와 인형",
                items=[
                    ProductItem(name="마스코트 8종", price="각 2,100엔", image=a),
                    ProductItem(name="인형 3종", price="각 2,800엔", image=b),
                ],
            ),
        ],
    )
    html = build_html(deck, "kitsch")
    assert "wide-items" in html and "--ar: 2.05" in html
    r = HtmlCardRenderer(style="kitsch")
    try:
        r.render(deck, tmp_path / "out")
        assert r.last_overflow == []
    finally:
        r.close()


def test_final_card_review_records_cause_and_reedits_once(tmp_path, monkeypatch) -> None:
    """완성 카드 검토(S18 프로세스): 문제는 원인 단계별로 남기고, 소재·기획·표지 배치 문제면 한 번만 다시 편집.
    글 문제만 있으면 사람에게(다시 편집하지 않음)."""
    from nsm.domain.models import BodySlide, CardDeck, CoverSlide, DeckBrand, DeckPersona
    from nsm.engine import card_review as CR
    from nsm.engine import picks as P

    small = _img(tmp_path, "small.jpg", (886, 432))
    deck = CardDeck(
        template="editorial",
        brand=DeckBrand(name="b", primary_color="#112233", accent_color="#445566"),
        persona=DeckPersona(name="p", handle="@p"),
        slides=[CoverSlide(title="t", background=small), BodySlide(heading="h", body="b")],
    )
    reasons = {"c1": "오늘의 추천; 관점: 굿즈 디테일"}
    content = SimpleNamespace(
        id="c1",
        is_ad=False,
        generated=SimpleNamespace(template="editorial", deck=deck),
        review_reason=reasons["c1"],
    )
    prev = tmp_path / "previews" / "c1"
    prev.mkdir(parents=True)
    Image.new("RGB", (10, 10)).save(prev / "slide-01.png")
    (prev / "overflow.txt").write_text("2")

    answers = [
        {
            "slides": [
                {
                    "slide": 1,
                    "problems": [{"part": "plan", "note": "표지에 가로 단체 사진이라 상품이 작음"}],
                }
            ]
        }
    ]

    class Reviewer:
        def review_cards(self, paths, brief):
            assert "관점: 굿즈 디테일" in brief and "코드가 잰 문제" in brief
            return answers[0]

    reedits = []
    monkeypatch.setattr(P, "reedit_info", lambda ctx, cid, feedback=None: reedits.append(feedback))
    monkeypatch.setattr("nsm.engine.publish.labeled_deck", lambda c: c.generated.deck)

    def get(cid):
        return SimpleNamespace(**{**content.__dict__, "review_reason": reasons[cid]})

    ctx = SimpleNamespace(
        text_generator=Reviewer(),
        media_dir=tmp_path,
        repo=SimpleNamespace(
            get_content=get, set_review_reason=lambda cid, r: reasons.__setitem__(cid, r)
        ),
    )
    assert "다시 편집" in CR.run_card_review(ctx, "c1")
    assert "완성 카드 검토: 1장 기획: 표지에 가로 단체 사진이라 상품이 작음" in reasons["c1"]
    assert (
        "완성 카드 검토: 수치: 2장 글 넘침" in reasons["c1"]
        and "배경 사진 886×432" in reasons["c1"]
    )
    assert reedits == [["1장 기획: 표지에 가로 단체 사진이라 상품이 작음"]]
    # 두 번째 검토에서는 다시 편집하지 않는다(한 번만), 예전 검토 줄은 새 결과로 바뀐다
    answers[0] = {
        "slides": [
            {"slide": 2, "problems": [{"part": "text", "note": "제목이 '수량/제한'으로 갈림"}]}
        ]
    }
    CR.run_card_review(ctx, "c1")
    assert (
        len(reedits) == 1 and "2장 글: 제목이" in reasons["c1"] and "1장 기획" not in reasons["c1"]
    )


def test_cover_gets_the_photo_that_fills_it_best_when_writer_leaves_it_empty(
    app_config, tmp_path
) -> None:
    """쓸 사진이 있으면 표지는 사진(사용자 피드백). 원고가 비우면 표지를 가장 잘 채우는 사진: 가로 단체 사진보다 정사각 사진."""
    wide = ObservedPhoto(
        path=_img(tmp_path, "w.jpg", (886, 432)),
        kind="official",
        size=[886, 432],
        subject=[2, 5, 96, 90],
    )
    square = ObservedPhoto(
        path=_img(tmp_path, "s.jpg", (838, 848)), kind="official", size=[838, 848]
    )
    thumb = ObservedPhoto(
        path=_img(tmp_path, "t.jpg", (2000, 2500)), kind="designed", size=[2000, 2500]
    )
    out = _out([_slide(heading="h", body="b")], cover_photo=-1)
    deck = build_deck(
        "editorial", out, app_config.brand, app_config.persona, _brief([wide, square, thumb])
    )
    assert deck.slides[0].background == square.path  # 남의 썸네일은 크더라도 쓰지 않는다
