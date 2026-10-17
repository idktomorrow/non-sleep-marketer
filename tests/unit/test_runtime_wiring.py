"""실행 조립(build_context): 렌더러가 지금 설정의 카드 스타일을 따른다 (S8, ADR 0036)."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from nsm.config.schema import AppConfig
from nsm.config.secrets import MemorySecretStore
from nsm.store.db import Database


def test_renderer_reads_card_style_from_current_config(
    app_config: AppConfig, nsm_home: Path, monkeypatch
) -> None:
    import nsm.render as render
    from nsm.runtime import build_context

    seen: dict = {}

    class Fake:
        def render(self, deck, out_dir):
            return []

    def fake_get_renderer(style="clean", character=None):
        seen["style"] = style
        seen["character"] = character
        return Fake()

    monkeypatch.setattr(render, "get_renderer", fake_get_renderer)
    s = app_config.settings
    cfg = app_config.model_copy(
        update={
            "settings": s.model_copy(
                update={
                    "collect": s.collect.model_copy(
                        update={"naver": False, "google_trends": False, "community": False}
                    )
                }
            )
        }
    )
    nsm_home.mkdir(parents=True)
    db = Database(nsm_home / "t.duckdb")
    try:
        with ExitStack() as stack:
            ctx = build_context(cfg, db, stack, secrets=MemorySecretStore())
            assert callable(seen["style"]) and seen["style"]() == "clean"
            # 대시보드 설정 저장(set_config)처럼 ctx.config를 바꾸면 다음 렌더부터 새 스타일
            ctx.config = cfg.model_copy(
                update={"brand": cfg.brand.model_copy(update={"card_style": "photo"})}
            )
            assert seen["style"]() == "photo"
            # 캐릭터도 렌더할 때마다 지금 설정에서 읽는다(C17)
            assert seen["character"]().cover.name == "parrot.png"
            ctx.config = cfg.model_copy(
                update={"brand": cfg.brand.model_copy(update={"character": "none"})}
            )
            assert seen["character"]().cover is None and not seen["character"]().drawn
    finally:
        db.close()
