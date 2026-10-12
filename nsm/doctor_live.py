"""`nsm doctor --live`: 실제 API를 한 번씩 호출해 키와 연결을 확인한다 (배치 B).

- 비용이 아주 조금 든다(분류 1회 수준). 이미지 생성은 비싸서 `--with-image`일 때만.
- 모델은 사용자 설정(config.yaml의 settings.models)을 따른다. 설정이 없으면 기본값.
- 키가 없으면 WARN. 선택 기능(MotherDuck, OpenAI)이 실패해도 앱은 대체 경로로 돌기 때문에 WARN,
  필수(Claude)가 실패하면 FAIL.
- 키를 환경 변수에서 읽었는지 keyring에서 읽었는지 함께 표시한다.
- 실제 호출 비용은 DB의 costs 테이블에 ref="doctor"로 기록한다(배치 Q1). `nsm run`이 켜져 있어
  DB를 열 수 없으면 기록 없이 점검만 한다(DuckDB는 한 프로세스만 연다).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from nsm.config.schema import ModelSettings
from nsm.config.secrets import ENV_NAMES, KeyringSecretStore, SecretStore
from nsm.doctor import Check
from nsm.ports import CostSink

PROBE_TEXT = "핸드드립 커피의 적정 물 온도는 90~96도로 알려져 있다."
PROBE_LABELS = ["relevant", "irrelevant"]
PROBE_INSTRUCTION = "이 글이 커피와 관련 있는가?"


def key_source(key: str) -> str:
    return "환경 변수" if any(os.environ.get(n) for n in ENV_NAMES.get(key, ())) else "keyring"


class _Tagged:
    """비용 기록에 ref="doctor"를 붙인다(대시보드에서 점검 비용을 구분)."""

    def __init__(self, sink: CostSink) -> None:
        self.sink = sink

    def record_cost(self, kind, provider: str, usd: float, ref: str = "") -> None:
        self.sink.record_cost(kind, provider, usd, f"doctor:{ref}" if ref else "doctor")


@contextlib.contextmanager
def cost_sink() -> Iterator[CostSink | None]:
    """설정된 DB에 비용을 기록한다. 열 수 없으면(`nsm run` 실행 중 등) None."""
    try:
        from nsm.config import paths
        from nsm.domain.clock import SystemClock
        from nsm.store.db import Database
        from nsm.store.repo import Repository

        db = Database(paths.db_path())
    except Exception:  # noqa: BLE001  점검은 기록 없이도 한다
        yield None
        return
    try:
        yield _Tagged(Repository(db, SystemClock()))
    finally:
        db.close()


def _anthropic(secrets: SecretStore, model: str, sink: CostSink | None = None) -> Check:
    name = "Claude API (실호출)"
    key = secrets.get("anthropic_api_key")
    if not key:
        return Check(name, "WARN", "키 없음: `nsm init`으로 입력")
    src = key_source("anthropic_api_key")
    try:
        import anthropic

        from nsm.adapters.classifier.claude import ClaudeClassifier

        c = ClaudeClassifier(anthropic.Anthropic(api_key=key), model, cost_sink=sink).classify(
            PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION
        )
        return Check(name, "OK", f"{model} → {c.label} ({c.confidence:.2f}) · 키: {src}")
    except Exception as e:  # noqa: BLE001
        return Check(name, "FAIL", f"{type(e).__name__}: {str(e)[:160]} · 키: {src}")


def _jev(secrets: SecretStore, sink: CostSink | None = None) -> Check:
    name = "MotherDuck jev (실호출)"
    token = secrets.get("motherduck_token")
    if not token:
        return Check(name, "WARN", "토큰 없음: Claude 분류기로 대신 동작")
    src = key_source("motherduck_token")
    try:
        from nsm.adapters.classifier.jev import JevClassifier

        c = JevClassifier(token, cost_sink=sink).classify(
            PROBE_TEXT, PROBE_LABELS, PROBE_INSTRUCTION
        )
        return Check(name, "OK", f"prompt_jev → {c.label} ({c.confidence:.2f}) · 키: {src}")
    except Exception as e:  # noqa: BLE001  (토큰은 JevClassifier가 오류 메시지에서 지운다)
        return Check(name, "WARN", f"실패, Claude 분류기로 대신 동작: {str(e)[:160]} · 키: {src}")


def _openai(secrets: SecretStore, model: str, out_dir: Path, sink: CostSink | None = None) -> Check:
    name = "OpenAI 이미지 (실호출)"
    key = secrets.get("openai_api_key")
    if not key:
        return Check(name, "WARN", "키 없음: 템플릿 배경으로 동작")
    src = key_source("openai_api_key")
    try:
        from nsm.adapters.generator.image_api import OpenAIImageGenerator

        OpenAIImageGenerator(key, model, out_dir, cost_sink=sink).generate_background(
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
    sink: CostSink | None = None,
) -> list[Check]:
    """sink를 넘기면 호출 비용을 기록한다. CLI(`nsm doctor --live`)가 `cost_sink()`로 연 DB를 넘긴다."""
    secrets = secrets or KeyringSecretStore()
    m = models or _models()
    return _run(secrets, with_image, m, sink)


def _run(
    secrets: SecretStore, with_image: bool, m: ModelSettings, sink: CostSink | None
) -> list[Check]:
    checks = [_anthropic(secrets, m.classifier, sink), _jev(secrets, sink)]
    if with_image:
        checks.append(_openai(secrets, m.image, Path(tempfile.gettempdir()) / "nsm-doctor", sink))
    return checks
