"""`nsm doctor --live`: 실제 API를 한 번씩 호출해 키와 연결을 확인한다 (배치 B).

비용이 아주 조금 든다(분류 1회 수준). 이미지 생성은 비싸서 `--with-image`일 때만.
키가 없는 항목은 SKIP으로 표시하고 넘어간다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from nsm.config.secrets import KeyringSecretStore, SecretStore
from nsm.doctor import Check

PROBE_TEXT = "핸드드립 커피의 적정 물 온도는 90~96도로 알려져 있다."
PROBE_LABELS = ["relevant", "irrelevant"]
PROBE_INSTRUCTION = "이 글이 커피와 관련 있는가?"


def _anthropic(secrets: SecretStore, model: str) -> Check:
    key = secrets.get("anthropic_api_key")
    if not key:
        return Check("Claude API (실호출)", "WARN", "키 없음: `nsm init`으로 입력")
    try:
        import anthropic

        from nsm.adapters.classifier.claude import ClaudeClassifier

        c = ClaudeClassifier(anthropic.Anthropic(api_key=key), model).classify(
            PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION
        )
        return Check("Claude API (실호출)", "OK", f"{model} → {c.label} ({c.confidence:.2f})")
    except Exception as e:  # noqa: BLE001
        return Check("Claude API (실호출)", "FAIL", f"{type(e).__name__}: {str(e)[:200]}")


def _jev(secrets: SecretStore) -> Check:
    token = secrets.get("motherduck_token")
    if not token:
        return Check("MotherDuck jev (실호출)", "WARN", "토큰 없음: Claude 분류기로 대신 동작")
    try:
        from nsm.adapters.classifier.jev import JevClassifier

        c = JevClassifier(token).classify(PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION)
        return Check(
            "MotherDuck jev (실호출)", "OK", f"prompt_jev → {c.label} ({c.confidence:.2f})"
        )
    except Exception as e:  # noqa: BLE001  (토큰은 JevClassifier가 오류 메시지에서 지운다)
        return Check("MotherDuck jev (실호출)", "FAIL", str(e)[:200])


def _openai(secrets: SecretStore, model: str, out_dir: Path) -> Check:
    key = secrets.get("openai_api_key")
    if not key:
        return Check("OpenAI 이미지 (실호출)", "WARN", "키 없음: 템플릿 배경으로 동작")
    try:
        from nsm.adapters.generator.image_api import OpenAIImageGenerator

        p = OpenAIImageGenerator(key, model, out_dir).generate_background(
            "calm coffee morning", 1080, 1350
        )
        return Check("OpenAI 이미지 (실호출)", "OK", f"{model} → {p}")
    except Exception as e:  # noqa: BLE001
        return Check("OpenAI 이미지 (실호출)", "FAIL", str(e)[:200])


def run_live_checks(
    secrets: SecretStore | None = None,
    with_image: bool = False,
    classifier_model: str = "claude-haiku-5-5",
    image_model: str = "gpt-image-2.5-flare",
) -> list[Check]:
    secrets = secrets or KeyringSecretStore()
    checks = [_anthropic(secrets, classifier_model), _jev(secrets)]
    if with_image:
        checks.append(_openai(secrets, image_model, Path(tempfile.gettempdir()) / "nsm-doctor"))
    return checks
