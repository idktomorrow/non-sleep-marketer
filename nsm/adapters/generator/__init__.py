"""생성기 조립(S13 제안): 글 공급자는 설정 `text_provider`(auto/openai/claude)와 키로 고른다.

- auto: Claude 키 → Claude, 아니면 OpenAI 키 → OpenAI(기존 사용자 동작 유지). 명시하면 그 공급자만 쓰고
  키가 없으면 None + 이유(조용히 다른 공급자로 바꾸지 않는다).
- 이미지: 표지 배경(ImageGenerator)은 `image_generation` auto/on/off 그대로, 범용 제작기(ImageStudio)는
  OpenAI 키가 있을 때만 만들고 실제 호출 여부는 제작 모드(S15)가 정한다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nsm.adapters.claude_common import GENERATION_TIMEOUT_S, MAX_RETRIES
from nsm.adapters.generator.image_api import NullImageGenerator, OpenAIImageGenerator
from nsm.config.schema import AppConfig
from nsm.config.secrets import SecretStore
from nsm.ports import CostSink, ImageGenerator, ImageStudio, TextGenerator

log = logging.getLogger(__name__)
NO_KEY_HINT = "설정 → AI 공급자·키에서 등록하거나 `nsm init`"


def choose_text_provider(cfg: AppConfig, secrets: SecretStore) -> tuple[str | None, str]:
    """(공급자 이름 | None, 이유). 공급자를 못 고르면 이유를 사람이 읽을 말로."""
    pref = cfg.settings.text_provider
    has_claude = bool(secrets.get("anthropic_api_key"))
    has_openai = bool(secrets.get("openai_api_key"))
    if pref == "claude":
        return ("claude", "") if has_claude else (None, f"Claude API 키가 없음({NO_KEY_HINT})")
    if pref == "openai":
        return ("openai", "") if has_openai else (None, f"OpenAI API 키가 없음({NO_KEY_HINT})")
    if has_claude:
        return "claude", ""
    if has_openai:
        return "openai", ""
    return None, f"AI 텍스트 공급자 키(Claude 또는 OpenAI)가 없음({NO_KEY_HINT})"


def make_text_generator(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink
) -> TextGenerator | None:
    gen, _ = make_text_generator_with_reason(cfg, secrets, cost_sink)
    return gen


def make_text_generator_with_reason(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink
) -> tuple[TextGenerator | None, str]:
    provider, reason = choose_text_provider(cfg, secrets)
    include_blog = "naver_blog" in cfg.settings.enabled_platforms
    if provider == "claude":
        import anthropic

        from nsm.adapters.generator.claude_text import ClaudeTextGenerator

        return ClaudeTextGenerator(
            # 작업 스레드가 하나라 생성이 멈추면 게시·답글도 멈춘다. SDK 기본 대기(10분)는 너무 길다(C24). 재시도 5번은 순간 끊김용
            anthropic.Anthropic(
                max_retries=MAX_RETRIES,
                timeout=GENERATION_TIMEOUT_S,
                api_key=secrets.get("anthropic_api_key"),
            ),
            cfg.settings.models.generation,
            cost_sink,
            include_blog=include_blog,
            tz=cfg.settings.timezone,
        ), ""
    if provider == "openai":
        import hashlib

        from nsm.adapters.generator.openai_text import OpenAITextGenerator
        from nsm.adapters.openai_common import OpenAIClient

        handle = cfg.persona.handle or cfg.persona.name
        return OpenAITextGenerator(
            OpenAIClient(secrets.get("openai_api_key") or "", timeout=GENERATION_TIMEOUT_S),
            cfg.settings.models.openai_text,
            cost_sink,
            effort=cfg.settings.models.openai_text_effort,
            include_blog=include_blog,
            tz=cfg.settings.timezone,
            safety_identifier=hashlib.sha256(handle.encode()).hexdigest()[:32],
        ), ""
    return None, reason


def make_image_generator(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink, media_dir: Path
) -> ImageGenerator:
    mode = cfg.settings.image_generation
    key = secrets.get("openai_api_key")
    if mode == "off" or (mode == "auto" and not key):
        return NullImageGenerator()
    if not key:
        # 이미지 하나 때문에 모든 잡이 멈추면 안 된다: 경고하고 템플릿 배경으로
        log.warning(
            "image_generation=on인데 OpenAI API 키가 없어 템플릿 배경을 씁니다 (%s)", NO_KEY_HINT
        )
        return NullImageGenerator()
    return OpenAIImageGenerator(key, cfg.settings.models.image, media_dir, cost_sink)


def make_image_studio(
    cfg: AppConfig, secrets: SecretStore, cost_sink: CostSink, media_dir: Path
) -> ImageStudio | None:
    """범용 이미지 제작기(S13). 키가 없으면 None — 호출자가 "키 없음"을 사람에게 보이고 대체하지 않는다."""
    key = secrets.get("openai_api_key")
    if not key:
        return None
    from nsm.adapters.generator.image_api import OpenAIImageStudio
    from nsm.adapters.openai_common import OpenAIClient

    return OpenAIImageStudio(
        OpenAIClient(key, timeout=150.0),
        cfg.settings.models.image,
        media_dir / "studio",
        cost_sink,
        default_quality=cfg.settings.models.image_quality,
    )
