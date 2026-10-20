"""설정 화면 카드 스타일 견본(S8): 지금 색·페르소나로 그린 표지·본문 장을 고르는 칸 옆에 보여 준다."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nsm.app.server import AppState, create_app
from nsm.app.style_preview import StylePreviewError, sample_deck, style_preview_png
from nsm.store.repo import Repository
from tests.app.test_dashboard import _run_now

PNG = b"\x89PNG\r\n\x1a\n"


class SampleRenderer:
    """render_sample만 흉내 낸다: 호출을 기록하고 장마다 PNG 머리만 쓴다."""

    version = "tv:clean"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def render(self, deck, out_dir):  # 포트
        return []

    def render_sample(self, deck, out_dir: Path, style: str) -> list[Path]:
        self.calls.append((style, deck.brand.primary_color, deck.brand.accent_color))
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(1, len(deck.slides) + 1):
            p = out_dir / f"slide-{i:02d}.png"
            p.write_bytes(PNG)
            paths.append(p)
        return paths


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    renderer = SampleRenderer()
    state = AppState(
        repo=Repository(db, clock),
        clock=clock,
        get_config=lambda: app_config,
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
        renderer=renderer,
    )
    return {"client": TestClient(create_app(state)), "renderer": renderer, "state": state}


def test_settings_page_shows_style_samples(env) -> None:
    html = env["client"].get("/settings").text
    for style in ("clean", "photo", "illust", "editorial"):
        assert f'value="{style}"' in html
        assert f"/settings/style-preview/{style}/1.png?primary=%231E3A5F" in html
        assert f"/settings/style-preview/{style}/2.png" in html


def test_style_sample_png_uses_colors_and_is_cached(env) -> None:
    c = env["client"]
    r = c.get(
        "/settings/style-preview/illust/1.png", params={"primary": "#FFD84D", "accent": "#ff6b6b"}
    )
    assert (
        r.status_code == 200
        and r.content.startswith(PNG)
        and r.headers["content-type"] == "image/png"
    )
    assert env["renderer"].calls == [("illust", "#FFD84D", "#FF6B6B")]
    # 같은 스타일·색이면 2장째도 다시 그리지 않는다
    assert (
        c.get(
            "/settings/style-preview/illust/2.png",
            params={"primary": "#FFD84D", "accent": "#FF6B6B"},
        ).status_code
        == 200
    )
    assert len(env["renderer"].calls) == 1
    # 색을 비우면 저장된 브랜드 색
    assert c.get("/settings/style-preview/photo/1.png").status_code == 200
    assert env["renderer"].calls[-1] == ("photo", "#1E3A5F", "#F2A541")


@pytest.mark.parametrize(
    "path, params",
    [
        ("/settings/style-preview/neon/1.png", {}),
        ("/settings/style-preview/photo/3.png", {}),
        ("/settings/style-preview/photo/1.png", {"primary": "red"}),
        ("/settings/style-preview/photo/1.png", {"accent": "#12345G"}),
        ("/settings/style-preview/photo/1.png", {"primary": '#000"><script>'}),
    ],
)
def test_bad_sample_requests_are_rejected(env, path: str, params: dict) -> None:
    assert env["client"].get(path, params=params).status_code == 400
    assert env["renderer"].calls == []


def test_without_sample_renderer_page_has_no_thumbs(env) -> None:
    env["state"].renderer = None
    html = env["client"].get("/settings").text
    assert 'name="card_style"' in html and 'class="style-thumb"' not in html
    assert env["client"].get("/settings/style-preview/clean/1.png").status_code == 404


def test_old_samples_are_pruned(app_config, tmp_path: Path, monkeypatch) -> None:
    import nsm.app.style_preview as sp

    monkeypatch.setattr(sp, "MAX_KEPT", 3)
    r = SampleRenderer()
    for i in range(6):
        style_preview_png(r, app_config, tmp_path, "clean", 1, f"#00000{i}", "#FFFFFF")
    assert len(list((tmp_path / "style-previews").iterdir())) == 3
    with pytest.raises(StylePreviewError):
        style_preview_png(r, app_config, tmp_path, "clean", 1, "#GGGGGG", "#FFFFFF")


def test_sample_deck_uses_persona_and_ad_label(app_config) -> None:
    deck = sample_deck(app_config, "#111111", "#222222")
    assert deck.persona.name == app_config.persona.name
    assert deck.labels.virtual_person and deck.labels.ad == app_config.persona.ad_label
    assert [s.kind for s in deck.slides] == ["cover", "list", "outro"]


def test_editorial_sample_has_bundled_photo_on_visible_slides(app_config) -> None:
    """사진 우선형(S10) 견본은 동봉 사진을 표지·본문(패널) 장에 깐다. 다른 스타일 견본은 사진이 없다."""
    deck = sample_deck(app_config, "#111111", "#222222", "editorial")
    bgs = [s.background for s in deck.slides]
    # 화면에 보이는 1·2장(표지·설명 패널 본문)에만 사진. 안 보이는 마지막 장은 사진 없이(그리는 값 절약)
    assert [s.kind for s in deck.slides] == ["cover", "body", "outro"]
    assert bgs[0] and bgs[0] == bgs[1] and bgs[2] is None
    assert Path(bgs[0]).is_file() and "sample-photo" in bgs[0]
    assert all(s.background is None for s in sample_deck(app_config, "#111111", "#222222").slides)


def test_editorial_sample_cache_key_tracks_photo_file(
    app_config, tmp_path: Path, monkeypatch
) -> None:
    """견본 사진 파일 내용이 바뀌면 editorial 견본을 다시 그린다(템플릿 지문은 사진을 안 본다)."""
    import nsm.app.style_preview as sp

    r = SampleRenderer()
    style_preview_png(r, app_config, tmp_path, "editorial", 1)
    style_preview_png(r, app_config, tmp_path, "editorial", 1)
    assert len(r.calls) == 1
    monkeypatch.setattr(sp, "_photo_fingerprint", lambda: "changed")
    style_preview_png(r, app_config, tmp_path, "editorial", 1)
    assert len(r.calls) == 2
