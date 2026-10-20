"""카드 편집 화면(M16 #35): 슬라이드 문구 수정 → 덱 반영·PNG 폐기, 배경 올리기, 미리보기 렌더."""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app import card_editor as E
from nsm.app.server import AppState, create_app
from nsm.domain.models import Content, ContentBrief
from nsm.store.repo import Repository
from tests.fakes import _ONE_PX_PNG, FakeRenderer, FakeTextGenerator, load_source_items


def _run_now(fn) -> Future:
    f: Future = Future()
    try:
        f.set_result(fn())
    except Exception as e:  # noqa: BLE001
        f.set_exception(e)
    return f


class OverflowRenderer(FakeRenderer):
    last_overflow = [2]


def _content(app_config, cid: str, template: str, created_at) -> Content:
    gen = FakeTextGenerator().generate_content(
        ContentBrief(topic="핸드드립", template=template, sources=load_source_items()[:2]),
        app_config.persona,
        app_config.brand,
    )
    return Content(
        id=cid,
        status="pending_review",
        generated=gen,
        relevance_confidence=0.9,
        created_at=created_at,
    )


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    repo = Repository(db, clock)
    renderer = FakeRenderer()
    state = AppState(
        repo=repo,
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
        renderer=renderer,
    )
    for item in load_source_items():
        repo.insert_source_item(item)
    for cid, template in (("c1", "info"), ("c2", "list")):
        repo.insert_content(_content(app_config, cid, template, clock.now()))
    return {
        "client": TestClient(create_app(state)),
        "repo": repo,
        "state": state,
        "renderer": renderer,
    }


def _post(env, url: str, files=None, **data):
    return env["client"].post(
        url, data={"csrf": env["state"].csrf_token, **data}, files=files, follow_redirects=False
    )


def _rendered(env, cid: str) -> Path:
    d = env["state"].media_dir / "cards" / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "slide-01.png").write_bytes(_ONE_PX_PNG)
    p = env["state"].media_dir / "previews" / cid
    p.mkdir(parents=True, exist_ok=True)
    (p / "slide-01.png").write_bytes(_ONE_PX_PNG)
    return d


# ---------------------------------------------------------------- 문구 수정
def test_edit_every_slide_updates_deck_and_drops_pngs(env) -> None:
    cards = _rendered(env, "c1")
    deck = env["repo"].get_content("c1").generated.deck
    body_i = next(i for i, s in enumerate(deck.slides) if s.kind == "body")
    outro_i = len(deck.slides) - 1
    r = _post(
        env,
        "/review/c1/edit",
        s0_title="새 표지\n두 번째 줄",
        s0_subtitle="새 부제",
        **{
            f"s{body_i}_heading": "새 소제목",
            f"s{body_i}_body": "새 본문입니다.",
            f"s{body_i}_bullets": "1. 첫째\n\n- 둘째\n",
            f"s{outro_i}_cta": "저장해 두세요",
        },
    )
    assert r.status_code == 303
    new = env["repo"].get_content("c1").generated.deck
    assert (new.slides[0].title, new.slides[0].subtitle) == ("새 표지\n두 번째 줄", "새 부제")
    b = new.slides[body_i]
    assert (b.heading, b.body, b.bullets) == ("새 소제목", "새 본문입니다.", ["첫째", "둘째"])
    assert new.slides[outro_i].cta == "저장해 두세요"
    assert new.slides[outro_i].sources == deck.slides[outro_i].sources  # 출처는 그대로
    assert not list(cards.glob("slide-*.png"))
    assert not list((env["state"].media_dir / "previews" / "c1").glob("slide-*.png"))


def test_fields_not_in_form_are_kept(env) -> None:
    before = env["repo"].get_content("c1").generated
    _post(env, "/review/c1/edit", instagram_caption="새 캡션")
    after = env["repo"].get_content("c1").generated
    assert after.instagram_caption == "새 캡션"
    assert after.deck == before.deck and after.threads_text == before.threads_text


def test_old_form_cover_title_still_works(env) -> None:
    _post(env, "/review/c1/edit", cover_title="예전 폼 표지")
    assert env["repo"].get_content("c1").generated.deck.slides[0].title == "예전 폼 표지"


def test_invalid_edit_is_400_and_nothing_saved(env) -> None:
    deck = env["repo"].get_content("c2").generated.deck
    li = next(i for i, s in enumerate(deck.slides) if s.kind == "list")
    r = _post(env, "/review/c2/edit", **{f"s{li}_bullets": "\n  \n"})  # 목록은 1개 이상
    assert r.status_code == 400 and f"{li + 1}장" in r.json()["detail"]
    assert env["repo"].get_content("c2").generated.deck == deck


def test_only_pending_review_is_editable(env) -> None:
    env["repo"].db.execute("UPDATE contents SET status = 'approved' WHERE id = 'c1'")
    assert _post(env, "/review/c1/edit", s0_title="x").status_code == 409


def test_csrf_required_for_edit_and_upload(env) -> None:
    c = env["client"]
    assert c.post("/review/c1/edit", data={"csrf": "x", "s0_title": "y"}).status_code == 403
    files = {"file": ("a.png", _ONE_PX_PNG, "image/png")}
    assert (
        c.post("/review/c1/slides/0/background", data={"csrf": "x"}, files=files).status_code == 403
    )


# ---------------------------------------------------------------- 배경
def test_upload_background_sets_slide_and_drops_pngs(env) -> None:
    cards = _rendered(env, "c1")
    r = _post(
        env,
        "/review/c1/slides/1/background",
        files={"file": ("../../evil.png", _ONE_PX_PNG, "image/png")},
    )
    assert r.status_code == 303
    deck = env["repo"].get_content("c1").generated.deck
    bg = Path(deck.slides[1].background)
    assert bg.is_file() and bg.parent == E.upload_dir(env["state"].media_dir, "c1")
    assert "evil" not in bg.name and bg.name.startswith("slide-02-")  # 사용자 파일 이름을 쓰지 않음
    assert deck.labels.ai_generated_image is False  # 사람이 올린 사진은 AI 생성이 아님
    assert not list(cards.glob("slide-*.png"))
    _post(env, "/review/c1/slides/1/background/remove")
    assert env["repo"].get_content("c1").generated.deck.slides[1].background is None


@pytest.mark.parametrize(
    ("data", "name"),
    [(b"<script>alert(1)</script>", "x.png"), (b"GIF89a....", "x.gif"), (b"%PDF-1.7", "x.jpg")],
)
def test_non_image_uploads_are_rejected(env, data: bytes, name: str) -> None:
    r = _post(env, "/review/c1/slides/0/background", files={"file": (name, data, "image/png")})
    assert r.status_code == 400
    assert env["repo"].get_content("c1").generated.deck.slides[0].background is None


def test_too_large_and_bad_index_are_rejected(env, monkeypatch) -> None:
    monkeypatch.setattr(E, "MAX_UPLOAD_BYTES", 10)
    big = _ONE_PX_PNG + b"0" * 100
    assert (
        _post(
            env, "/review/c1/slides/0/background", files={"file": ("a.png", big, "image/png")}
        ).status_code
        == 400
    )
    monkeypatch.undo()
    assert (
        _post(
            env,
            "/review/c1/slides/99/background",
            files={"file": ("a.png", _ONE_PX_PNG, "image/png")},
        ).status_code
        == 400
    )


def test_ai_label_follows_remaining_ai_backgrounds(env, app_config) -> None:
    media = env["state"].media_dir
    media.mkdir(parents=True, exist_ok=True)
    ai = media / "bg-01ABC.png"
    ai.write_bytes(_ONE_PX_PNG)
    gen = env["repo"].get_content("c1").generated
    gen = E.set_background(gen, 0, str(ai), media)
    assert gen.deck.labels.ai_generated_image is True
    photo = E.save_upload(media, "c1", 1, _ONE_PX_PNG)
    gen = E.set_background(gen, 1, str(photo), media)
    assert gen.deck.labels.ai_generated_image is True  # AI 배경이 남아 있으면 표기 유지
    gen = E.set_background(gen, 0, None, media)
    assert gen.deck.labels.ai_generated_image is False  # 사진만 남으면 끈다


@pytest.mark.parametrize(
    ("head", "ext"),
    [
        (b"\x89PNG\r\n\x1a\n0000", ".png"),
        (b"\xff\xd8\xff\xe0", ".jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp"),
        (b"BM", None),
    ],
)
def test_sniff_image(head: bytes, ext: str | None) -> None:
    assert E.sniff_image(head) == ext


# ---------------------------------------------------------------- 미리보기
def test_preview_renders_current_deck_and_shows_images(env) -> None:
    _post(env, "/review/c1/edit", s0_title="미리보기 표지")
    r = _post(env, "/review/c1/preview")
    assert r.status_code == 303
    rendered = env["renderer"].rendered[-1]
    assert rendered.slides[0].title == "미리보기 표지"
    assert rendered.labels.virtual_person is True  # 게시와 같은 표기로 그린다
    prev = env["state"].media_dir / "previews" / "c1"
    names = sorted(p.name for p in prev.glob("slide-*.png"))
    assert names and not list(
        (env["state"].media_dir / "cards" / "c1").glob("*.png")
    )  # 게시용과 따로
    html = env["client"].get("/review/c1").text
    assert f"/previews/c1/{names[0]}" in html and "넘치는 장 없음" in html
    assert env["client"].get(f"/previews/c1/{names[0]}").status_code == 200


def test_preview_reports_overflow(env) -> None:
    env["state"].renderer = OverflowRenderer()
    _post(env, "/review/c1/preview")
    assert "2장에 글이 넘칩니다" in env["client"].get("/review/c1").text


def test_preview_without_renderer_is_503(env) -> None:
    env["state"].renderer = None
    assert _post(env, "/review/c1/preview").status_code == 503
    assert "미리보기를 만들 수 없습니다" in env["client"].get("/review/c1").text


@pytest.mark.parametrize(
    "path",
    [
        "/previews/c1/x..png",
        "/previews/c1/slide-..%5Cx.png",
        "/previews/nope/slide-01.png",
        "/previews/c1/x.txt",
    ],
)
def test_preview_image_route_rejects_bad_paths(env, path: str) -> None:
    assert env["client"].get(path).status_code == 404


def test_editor_page_shows_every_slide(env) -> None:
    html = env["client"].get("/review/c2").text
    deck = env["repo"].get_content("c2").generated.deck
    for i, _ in enumerate(deck.slides):
        assert f"/review/c2/slides/{i}/background" in html
    assert 'name="s0_title"' in html and '_sources"' in html


def test_empty_required_text_is_rejected(env) -> None:
    before = env["repo"].get_content("c1").generated.deck
    r = _post(env, "/review/c1/edit", s0_title="   ")
    assert r.status_code == 400 and "1장 title" in r.json()["detail"]
    assert env["repo"].get_content("c1").generated.deck == before


def test_upload_size_limit_in_route(env, monkeypatch) -> None:
    from nsm.app import server

    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(E, "MAX_UPLOAD_BYTES", 10)
    big = _ONE_PX_PNG + b"0" * 100
    r = _post(env, "/review/c1/slides/0/background", files={"file": ("a.png", big, "image/png")})
    assert r.status_code == 400


def test_replacing_background_deletes_previous_upload(env) -> None:
    _post(
        env, "/review/c1/slides/1/background", files={"file": ("a.png", _ONE_PX_PNG, "image/png")}
    )
    first = Path(env["repo"].get_content("c1").generated.deck.slides[1].background)
    _post(
        env, "/review/c1/slides/1/background", files={"file": ("b.png", _ONE_PX_PNG, "image/png")}
    )
    second = Path(env["repo"].get_content("c1").generated.deck.slides[1].background)
    assert not first.exists() and second.exists()
    _post(env, "/review/c1/slides/1/background/remove")
    assert not second.exists()


def test_late_preview_uses_latest_text_and_records_overflow(env) -> None:
    """시간 초과로 요청이 먼저 끝나도, 늦게 도는 렌더가 최신 원고로 그리고 넘침 기록을 같이 남긴다."""
    queued = []
    env["state"].renderer = OverflowRenderer()
    env["state"].submit = lambda fn: (queued.append(fn), _pending())[1]
    r = _post(env, "/review/c1/preview")
    assert r.status_code == 504
    _post(env, "/review/c1/edit", s0_title="나중에 고친 표지")  # 그 사이에 고침
    queued[0]()  # 잡 스레드가 늦게 실행
    assert env["state"].renderer.rendered[-1].slides[0].title == "나중에 고친 표지"
    assert "2장에 글이 넘칩니다" in env["client"].get("/review/c1").text


def test_previews_without_overflow_record_are_hidden(env) -> None:
    _rendered(env, "c1")  # PNG만 있고 넘침 기록 없음(그리는 중)
    html = env["client"].get("/review/c1").text
    assert "/previews/c1/" not in html and "넘치는 장 없음" not in html


class _pending:
    def result(self, timeout=None):
        from concurrent.futures import TimeoutError as FutureTimeout

        raise FutureTimeout()


# ---------------------------------------------------------------- 출처 수정
def test_edit_sources_reaches_card_and_post_tail(env) -> None:
    from nsm.engine.publish import build_post

    deck = env["repo"].get_content("c1").generated.deck
    oi = len(deck.slides) - 1
    r = _post(
        env,
        "/review/c1/edit",
        **{
            f"s{oi}_sources": "https://news.naver.com/a\n브랜드 제공 자료\n\nhttps://news.naver.com/a"
        },
    )
    assert r.status_code == 303
    c = env["repo"].get_content("c1")
    assert c.generated.deck.slides[oi].sources == ["https://news.naver.com/a", "브랜드 제공 자료"]
    post = build_post(c, "threads", [])
    assert post.text.rstrip().endswith("출처: 네이버 뉴스, 브랜드 제공 자료")


@pytest.mark.parametrize(
    ("value", "msg"),
    [
        ("\n \n", "1개 이상"),
        ("\n".join(f"https://a.com/{i}" for i in range(6)), "5개까지"),
        ("javascript:alert(1)", "http"),
        ("file:///C:/secret.txt", "http"),
        ("https://a.com/" + "x" * 300, "200자"),
    ],
)
def test_bad_sources_are_rejected(value: str, msg: str) -> None:
    with pytest.raises(E.EditError, match=msg):
        E.parse_sources(value)


def test_source_names_with_colon_and_space_are_allowed() -> None:
    assert E.parse_sources("출처: 브랜드 자료\nBrand: 제공 자료") == [
        "출처: 브랜드 자료",
        "Brand: 제공 자료",
    ]


def test_editor_shows_sources_box(env) -> None:
    deck = env["repo"].get_content("c1").generated.deck
    oi = len(deck.slides) - 1
    html = env["client"].get("/review/c1").text
    assert f'name="s{oi}_sources"' in html


def test_edit_after_approval_is_refused_at_the_database(env) -> None:
    """C24: 화면 검사 뒤 승인이 끼어들어도(동시 요청) 승인된 원고는 바뀌지 않는다."""
    from nsm.store.repo import InvalidTransitionError

    repo = env["repo"]
    c = repo.get_content("c1")
    repo.transition_content("c1", "approved", approved_by="human")
    with pytest.raises(InvalidTransitionError):
        repo.update_generated("c1", c.generated.model_copy(update={"threads_text": "바뀜"}))
    assert repo.get_content("c1").generated.threads_text == c.generated.threads_text
    r = _post(env, "/review/c1/edit", threads_text="바뀜")
    assert r.status_code == 409


def test_products_and_facts_fields_and_item_images(env) -> None:
    """S12: 상품 그리드·일정 패널 장의 글자를 고치고 상품 그림을 올리고 지운다."""
    from pathlib import Path

    from nsm.domain.models import CardDeck
    from tests.render.test_html_renderer import FIXTURES

    repo = env["repo"]
    c = repo.get_content("c1")
    deck = CardDeck.model_validate_json((FIXTURES / "editorial.json").read_text("utf-8"))
    repo.update_generated(
        "c1", c.generated.model_copy(update={"deck": deck, "template": "editorial"})
    )
    html = env["client"].get("/review/c1").text
    assert (
        'name="s2_item0_name"' in html
        and 'name="s3_row0_label"' in html
        and "/items/0/image" in html
    )
    r = _post(
        env,
        "/review/c1/edit",
        s2_item0_name="머그컵(수정)",
        s2_item0_price="19,000원",
        s3_row1_value="11월 2일부터",
    )
    assert r.status_code == 303
    d = repo.get_content("c1").generated.deck
    assert d.slides[2].items[0].name == "머그컵(수정)" and d.slides[2].items[0].price == "19,000원"
    assert d.slides[3].rows[1].value == "11월 2일부터"
    assert _post(env, "/review/c1/edit", s2_item1_name="").status_code == 400  # 이름은 비울 수 없다
    assert _post(env, "/review/c1/edit", s3_row0_label="").status_code == 400
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4b00000000049454e44ae426082"
    )
    r = env["client"].post(
        "/review/c1/slides/2/items/1/image",
        data={"csrf": env["state"].csrf_token},
        files={"file": ("x.png", png, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    img = repo.get_content("c1").generated.deck.slides[2].items[1].image
    assert img and Path(img).is_file() and "uploads/backgrounds/c1" in img.replace("\\", "/")
    assert not repo.get_content("c1").generated.deck.labels.ai_generated_image  # 사람이 올린 그림
    assert _post(env, "/review/c1/slides/2/items/1/image/remove").status_code == 303
    assert (
        repo.get_content("c1").generated.deck.slides[2].items[1].image is None
        and not Path(img).exists()
    )
    assert (
        _post(env, "/review/c1/slides/1/items/0/image/remove").status_code == 400
    )  # 상품 장이 아님
