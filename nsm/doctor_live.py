"""`nsm doctor --live`: 실제 API를 한 번씩 호출해 키와 연결을 확인한다 (배치 B).

- 비용이 아주 조금 든다(분류 1회 수준). 이미지 생성은 비싸서 `--with-image`일 때만.
- 모델은 사용자 설정(config.yaml의 settings.models)을 따른다. 설정이 없으면 기본값.
- 키가 없으면 WARN. 선택 기능(MotherDuck, OpenAI)이 실패해도 앱은 대체 경로로 돌기 때문에 WARN,
  필수(Claude)가 실패하면 FAIL.
- 키를 환경 변수에서 읽었는지 keyring에서 읽었는지 함께 표시한다.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from nsm.config.schema import ModelSettings
from nsm.config.secrets import ENV_NAMES, KeyringSecretStore, SecretStore
from nsm.doctor import Check

PROBE_TEXT = "핸드드립 커피의 적정 물 온도는 90~96도로 알려져 있다."
PROBE_LABELS = ["relevant", "irrelevant"]
PROBE_INSTRUCTION = "이 글이 커피와 관련 있는가?"


def key_source(key: str) -> str:
    return "환경 변수" if any(os.environ.get(n) for n in ENV_NAMES.get(key, ())) else "keyring"


def _anthropic(secrets: SecretStore, model: str) -> Check:
    name = "Claude API (실호출)"
    key = secrets.get("anthropic_api_key")
    if not key:
        return Check(name, "WARN", "키 없음: `nsm init`으로 입력")
    src = key_source("anthropic_api_key")
    try:
        import anthropic

        from nsm.adapters.classifier.claude import ClaudeClassifier

        c = ClaudeClassifier(anthropic.Anthropic(api_key=key), model).classify(
            PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION
        )
        return Check(name, "OK", f"{model} → {c.label} ({c.confidence:.2f}) · 키: {src}")
    except Exception as e:  # noqa: BLE001
        return Check(name, "FAIL", f"{type(e).__name__}: {str(e)[:160]} · 키: {src}")


def _jev(secrets: SecretStore) -> Check:
    name = "MotherDuck jev (실호출)"
    token = secrets.get("motherduck_token")
    if not token:
        return Check(name, "WARN", "토큰 없음: Claude 분류기로 대신 동작")
    src = key_source("motherduck_token")
    try:
        from nsm.adapters.classifier.jev import JevClassifier

        c = JevClassifier(token).classify(PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION)
        return Check(name, "OK", f"prompt_jev → {c.label} ({c.confidence:.2f}) · 키: {src}")
    except Exception as e:  # noqa: BLE001  (토큰은 JevClassifier가 오류 메시지에서 지운다)
        return Check(name, "WARN", f"실패, Claude 분류기로 대신 동작: {str(e)[:160]} · 키: {src}")


def _openai(secrets: SecretStore, model: str, out_dir: Path) -> Check:
    name = "OpenAI 이미지 (실호출)"
    key = secrets.get("openai_api_key")
    if not key:
        return Check(name, "WARN", "키 없음: 템플릿 배경으로 동작")
    src = key_source("openai_api_key")
    try:
        from nsm.adapters.generator.image_api import OpenAIImageGenerator

        OpenAIImageGenerator(key, model, out_dir).generate_background(
            "calm coffee morning", 1080, 1350
        )
        return Check(name, "OK", f"{model} → 이미지 1장 생성 · 키: {src}")
    except Exception as e:  # noqa: BLE001
        return Check(name, "WARN", f"실패, 템플릿 배경으로 동작: {str(e)[:160]} · 키: {src}")


def _models() -> ModelSettings:
    try:
        from nsm.config.store import load_config

        return load_config().settings.models
    except Exception:  # noqa: BLE001  설정 전에도 키 확인은 할 수 있게
        return ModelSettings()


def run_live_checks(
    secrets: SecretStore | None = None,
    with_image: bool = False,
    models: ModelSettings | None = None,
) -> list[Check]:
    secrets = secrets or KeyringSecretStore()
    m = models or _models()
    checks = [_anthropic(secrets, m.classifier), _jev(secrets)]
    if with_image:
        checks.append(_openai(secrets, m.image, Path(tempfile.gettempdir()) / "nsm-doctor"))
    return checks
