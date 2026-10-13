"""생성기 조립: Claude 키가 있으면 Claude, 없으면 생성 불가(None). 이미지는 auto/on/off."""

from __future__ import annotations

import logging
from pathlib import Path

from nsm.adapters.generator.image_api import NullImageGenerator, OpenAIImageGenerator
from nsm.config.schema import AppConfig
from nsm.config.secrets import SecretStore
from nsm.ports import CostSink, ImageGenerator, TextGenerator


def make_text_generator(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink
) -> TextGenerator | None:
    key = secrets.get("anthropic_api_key")
    if not key:
        return None
    import anthropic

    from nsm.adapters.generator.claude_text import ClaudeTextGenerator

    return ClaudeTextGenerator(
        anthropic.Anthropic(api_key=key),
        cfg.settings.models.generation,
        cost_sink,
        include_blog="naver_blog" in cfg.settings.enabled_platforms,
        tz=cfg.settings.timezone,
    )


def make_image_generator(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink, media_dir: Path
) -> ImageGenerator:
    mode = cfg.settings.image_generation
    key = secrets.get("openai_api_key")
    if mode == "off" or (mode == "auto" and not key):
        return NullImageGenerator()
    if not key:
        # 이미지 하나 때문에 모든 잡이 멈추면 안 된다: 경고하고 템플릿 배경으로
        logging.getLogger(__name__).warning(
            "image_generation=on인데 OpenAI API 키가 없어 템플릿 배경을 씁니다 (`nsm init`)"
        )
        return NullImageGenerator()
    return OpenAIImageGenerator(key, cfg.settings.models.image, media_dir, cost_sink)
