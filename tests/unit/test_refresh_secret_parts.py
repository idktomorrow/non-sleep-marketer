"""C35: 키를 바꾸면 키를 쓰는 부품(분류기·생성기·진단·이미지 읽기)을 재시작 없이 새로 만든다."""

from __future__ import annotations

from contextlib import ExitStack

from nsm.config.secrets import MemorySecretStore
from nsm.runtime import build_context, refresh_secret_parts


def test_refresh_switches_classifier_and_text_generator(db, app_config) -> None:
    app_config.settings.collect.naver = False
    secrets = MemorySecretStore({})
    with ExitStack() as stack:
        ctx = build_context(app_config, db, stack, secrets=secrets)
        assert ctx.classifier_name == "keyword" and ctx.diagnoser is None
        before_gen = ctx.text_generator
        secrets.set("gemini_api_key", "fake-gemini-key-EEEEEEEEEEEEEEEEEEEE")
        secrets.set("anthropic_api_key", "fake-claude-key-FFFFFFFFFFFFFFFFFFFF")
        read_image = refresh_secret_parts(ctx, secrets)
        assert ctx.classifier_name == "gemini"
        assert ctx.text_generator is not before_gen and ctx.diagnoser is not None
        upload = next(s for s in ctx.sources if s.name == "upload")
        assert upload.read_image is read_image
        secrets.delete("gemini_api_key")
        refresh_secret_parts(ctx, secrets)
        assert ctx.classifier_name == "claude"


def test_failed_refresh_keeps_old_parts(db, app_config, monkeypatch) -> None:
    """리뷰: 새 부품을 만들다 실패하면 반쯤 바뀐 상태 없이 예전 부품이 그대로 남는다."""
    import pytest

    import nsm.adapters.diagnosis as diagnosis

    app_config.settings.collect.naver = False
    secrets = MemorySecretStore({})
    with ExitStack() as stack:
        ctx = build_context(app_config, db, stack, secrets=secrets)
        before = (ctx.classifier_name, ctx.text_generator, ctx.relevance_classifier)
        secrets.set("gemini_api_key", "fake-gemini-key-HHHHHHHHHHHHHHHHHHHH")

        def boom(*a, **k):
            raise RuntimeError("diagnoser broke")

        monkeypatch.setattr(diagnosis, "make_diagnoser", boom)
        with pytest.raises(RuntimeError):
            refresh_secret_parts(ctx, secrets)
        assert (ctx.classifier_name, ctx.text_generator, ctx.relevance_classifier) == before
