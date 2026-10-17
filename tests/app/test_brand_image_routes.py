"""설정 화면 캐릭터·로고(C17, ADR 0046): 고르기, 올리기, 지우기, 보기."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from nsm.app.settings_form import form_values
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처


@pytest.fixture
def benv(env, tmp_path):  # noqa: F811
    env["state"].brand_dir = tmp_path / "brand"
    return env


def _drawing_png() -> bytes:
    img = Image.new("RGB", (300, 300), (251, 249, 244))
    ImageDraw.Draw(img).ellipse(
        (60, 60, 240, 240), fill=(250, 246, 236), outline=(30, 30, 30), width=10
    )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _upload(e, kind: str, data: bytes, clear: bool = True):
    return e["client"].post(
        "/settings/brand-image",
        data={
            "csrf": e["state"].csrf_token,
            "kind": kind,
            **({"clear_background": "on"} if clear else {}),
        },
        files={"file": ("c.png", data)},
        follow_redirects=False,
    )


def _cfg(e):
    return e["holder"]["cfg"]


def test_settings_shows_character_picker_and_upload(benv) -> None:
    html = benv["client"].get("/settings").text
    assert 'name="card_character" value="parrot" checked' in html  # 기본은 앵무새
    assert "/settings/character/hamster.png" in html
    assert 'name="character_ai_made"' in html
    assert 'id="brand-images"' in html and "직접 만든 그림만 올리세요" in html
    assert (
        benv["client"].get("/settings/character/parrot.png").headers["content-type"] == "image/png"
    )
    assert benv["client"].get("/settings/character/../../x.png").status_code == 404
    assert benv["client"].get("/settings/character/bogus.png").status_code == 404


def test_upload_section_hidden_without_folder(env) -> None:  # noqa: F811
    assert 'id="brand-images"' not in env["client"].get("/settings").text


def test_upload_character_switches_to_custom_and_clears_background(benv) -> None:
    r = _upload(benv, "character_cover", _drawing_png())
    assert r.status_code == 303 and "image_saved=character_cover" in r.headers["location"]
    b = _cfg(benv).brand
    assert b.character == "custom" and b.character_cover
    saved = Image.open(b.character_cover)
    assert saved.mode == "RGBA" and saved.getchannel("A").getpixel((0, 0)) < 255
    assert (
        '캐릭터를 "직접 올린 그림"으로 바꿨습니다' in benv["client"].get(r.headers["location"]).text
    )
    assert benv["client"].get("/settings/brand-image/character_cover.png").status_code == 200

    # 바꾸면 예전 파일은 지운다
    old = b.character_cover
    _upload(benv, "character_cover", _drawing_png())
    from pathlib import Path

    assert not Path(old).exists() and Path(_cfg(benv).brand.character_cover).exists()


def test_delete_last_custom_image_goes_back_to_parrot(benv) -> None:
    from pathlib import Path

    _upload(benv, "character_cover", _drawing_png())
    path = _cfg(benv).brand.character_cover
    r = _post(benv, "/settings/brand-image/delete", kind="character_cover")
    assert r.status_code == 303
    b = _cfg(benv).brand
    assert b.character_cover is None and b.character == "parrot" and not Path(path).exists()
    assert benv["client"].get("/settings/brand-image/character_cover.png").status_code == 404


def test_logo_upload_keeps_character(benv) -> None:
    _upload(benv, "logo_path", _drawing_png(), clear=False)
    b = _cfg(benv).brand
    assert b.logo_path and b.character == "parrot"
    assert Image.open(b.logo_path).getchannel("A").getpixel((0, 0)) == 255  # 로고 바탕은 그대로


def test_bad_upload_shows_reason(benv) -> None:
    r = _upload(benv, "character_cover", b"not an image")
    assert "image_error=" in r.headers["location"]
    assert "PNG·JPEG·WebP" in benv["client"].get(r.headers["location"]).text
    assert _cfg(benv).brand.character == "parrot"
    bad = benv["client"].post(
        "/settings/brand-image",
        data={"csrf": benv["state"].csrf_token, "kind": "../x"},
        files={"file": ("c.png", _drawing_png())},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert benv["client"].get("/settings/brand-image/persona.png").status_code == 404


def test_upload_needs_csrf(benv) -> None:
    r = benv["client"].post(
        "/settings/brand-image",
        data={"kind": "character_cover"},
        files={"file": ("c.png", _drawing_png())},
        follow_redirects=False,
    )
    assert r.status_code in (400, 403)


def test_choose_character_and_ai_label_in_settings_form(benv) -> None:
    cfg = _cfg(benv)
    form = form_values(cfg)
    form.update(card_character="hamster")
    form.pop("character_ai_made")  # 체크를 끈 상태
    assert _post(benv, "/settings", **form).status_code == 303
    b = _cfg(benv).brand
    assert b.character == "hamster" and b.character_ai_made is False
    assert _cfg(benv).persona.character == cfg.persona.character  # 인물상 칸과 섞이지 않는다

    form = form_values(_cfg(benv))
    form.update(card_character="custom")
    r = _post(benv, "/settings", **form)
    assert r.status_code == 400 and "직접 올린 그림이 아직 없습니다" in r.text


def test_old_form_without_character_keeps_values(benv) -> None:
    cfg = _cfg(benv)
    cfg = cfg.model_copy(
        update={
            "brand": cfg.brand.model_copy(update={"character": "none", "character_ai_made": False})
        }
    )
    benv["holder"]["cfg"] = cfg
    form = form_values(cfg)
    form.pop("card_character")
    form.pop("character_ai_made")
    assert _post(benv, "/settings", **form).status_code == 303
    assert (_cfg(benv).brand.character, _cfg(benv).brand.character_ai_made) == ("none", False)
