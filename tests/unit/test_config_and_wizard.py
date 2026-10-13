from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from nsm.config.schema import AppConfig
from nsm.config.secrets import KeyringSecretStore, MemorySecretStore
from nsm.config.store import (
    ConfigNotFoundError,
    format_windows,
    load_config,
    parse_windows,
    save_config,
)
from nsm.config.wizard import WizardAbortedError, wizard
from nsm.domain.labels import DEFAULT_BANNED_PATTERNS


class ScriptedIO:
    """준비한 답을 순서대로 돌려준다. 질문 문구로 답을 고르지 않으므로 순서가 곧 계약이다."""

    def __init__(self, prompts: list[str], confirms: list[bool]) -> None:
        self.prompts = list(prompts)
        self.confirms = list(confirms)
        self.out: list[str] = []
        self.asked: list[str] = []

    def echo(self, text: str) -> None:
        self.out.append(text)

    def prompt(self, text: str, default: str | None = None, hide_input: bool = False) -> str:
        self.asked.append(text)
        answer = self.prompts.pop(0)
        return answer if answer != "<default>" else (default or "")

    def confirm(self, text: str, default: bool = False) -> bool:
        self.asked.append(text)
        return self.confirms.pop(0)


NEW_ANSWERS = [
    # 브랜드: 이름, 업종, 제품, 메시지, 주색, 강조색
    "달빛로스터리",
    "스페셜티 커피",
    "원두, 드립백",
    "신선한 로스팅",
    "<default>",
    "<default>",
    # 페르소나: 활동시간, 이름, 핸들, 인물상, 말투, 주제, 금칙어
    "09:00-12:00, 18:00-23:00",
    "루나",
    "@luna.ai",
    "커피를 공부하는 AI 바리스타",
    "<default>",
    "핸드드립, 원두",
    "최고, 무조건",
    # 키 3개
    "sk-ant-test-123456",
    "",
    "",
]
# 동의, 광고 표기, 페르소나 켜기, 드라이런
NEW_CONFIRMS = [True, False, True, True]


def test_wizard_creates_config_and_stores_keys(nsm_home: Path) -> None:
    io = ScriptedIO(NEW_ANSWERS, NEW_CONFIRMS)
    secrets = MemorySecretStore()
    now = datetime(2026, 10, 11, 1, 0, tzinfo=UTC)
    cfg = wizard(io, secrets, now=now)

    saved = load_config()
    assert saved == cfg
    assert saved.risk_accepted_at == now
    assert saved.brand.products == ["원두", "드립백"]
    assert saved.persona.topics == ["핸드드립", "원두"]
    assert saved.persona.active_hours[1].start == time(18, 0)
    assert set(DEFAULT_BANNED_PATTERNS) <= set(saved.persona.banned_patterns)
    assert saved.settings.dry_run is True
    assert secrets.get("anthropic_api_key") == "sk-ant-test-123456"
    assert secrets.get("openai_api_key") is None
    # 키는 설정 파일에 쓰이지 않는다
    assert "sk-ant" not in (nsm_home / "config.yaml").read_text("utf-8")
    assert (nsm_home / "uploads").is_dir()


def test_wizard_aborts_without_risk_consent(nsm_home: Path) -> None:
    io = ScriptedIO([], [False])
    with pytest.raises(WizardAbortedError):
        wizard(io, MemorySecretStore())
    assert not (nsm_home / "config.yaml").exists()


def test_wizard_edit_mode_keeps_existing_values(nsm_home: Path) -> None:
    wizard(ScriptedIO(NEW_ANSWERS, NEW_CONFIRMS), MemorySecretStore())
    keep_all = ["<default>"] * 13 + ["", "", ""]
    cfg = wizard(ScriptedIO(keep_all, [True, False, True, True]), MemorySecretStore())
    assert cfg.brand.name == "달빛로스터리"
    assert cfg.persona.banned_words == ["최고", "무조건"]
    # 기본 금칙 패턴이 중복으로 쌓이지 않는다
    assert len(cfg.persona.banned_patterns) == len(DEFAULT_BANNED_PATTERNS)


def test_wizard_reasks_invalid_hours(nsm_home: Path) -> None:
    answers = NEW_ANSWERS[:6] + ["9시부터", *NEW_ANSWERS[6:]]
    io = ScriptedIO(answers, NEW_CONFIRMS)
    cfg = wizard(io, MemorySecretStore())
    assert len(cfg.persona.active_hours) == 2
    assert any("형식이 잘못" in o for o in io.out)


def test_config_roundtrip(tmp_path: Path, app_config: AppConfig) -> None:
    p = save_config(app_config, tmp_path / "c.yaml")
    assert load_config(p) == app_config


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_parse_windows() -> None:
    w = parse_windows("09:00-12:00,22:00-02:00")
    assert w[1].start == time(22) and w[1].end == time(2)
    assert format_windows(w) == "09:00-12:00, 22:00-02:00"
    with pytest.raises(ValueError):
        parse_windows("")
    with pytest.raises(ValueError):
        parse_windows("9-12")


def test_env_var_overrides_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NSM_ANTHROPIC_API_KEY", "from-env")
    assert KeyringSecretStore(service="nsm-test-unused").get("anthropic_api_key") == "from-env"


def test_wizard_reasks_bad_color_and_empty_topics(nsm_home: Path) -> None:
    answers = list(NEW_ANSWERS)
    answers[4:5] = ["blue", "#123456"]  # 주색: 잘못된 값 → 다시 묻기
    i = answers.index("핸드드립, 원두")
    answers[i : i + 1] = [" , ", "핸드드립"]  # 주제: 빈 목록 → 다시 묻기
    cfg = wizard(ScriptedIO(answers, NEW_CONFIRMS), MemorySecretStore())
    assert cfg.brand.primary_color == "#123456"
    assert cfg.persona.topics == ["핸드드립"]


def test_wizard_keeps_logo_and_survives_keyring_failure(nsm_home: Path) -> None:
    wizard(ScriptedIO(NEW_ANSWERS, NEW_CONFIRMS), MemorySecretStore())
    cfg = load_config()
    save_config(
        cfg.model_copy(
            update={"brand": cfg.brand.model_copy(update={"logo_path": Path("C:/logo.png")})}
        )
    )

    class BrokenStore(MemorySecretStore):
        def set(self, key: str, value: str) -> None:
            raise RuntimeError("no keyring backend")

    answers = ["<default>"] * 13 + ["sk-x", "", ""]
    cfg2 = wizard(ScriptedIO(answers, [True, False, True, True]), BrokenStore())
    assert cfg2.brand.logo_path == Path("C:/logo.png")
    assert load_config().brand.logo_path == Path("C:/logo.png")


def test_wizard_recovers_from_broken_config(nsm_home: Path) -> None:
    nsm_home.mkdir(parents=True, exist_ok=True)
    (nsm_home / "config.yaml").write_text("brand: [broken", encoding="utf-8")
    cfg = wizard(ScriptedIO(NEW_ANSWERS, NEW_CONFIRMS), MemorySecretStore())
    assert cfg.brand.name == "달빛로스터리"
    assert (nsm_home / "config.yaml.broken").exists()


def test_reply_interval_has_a_floor(app_config) -> None:
    import pydantic

    from nsm.config.schema import Settings

    assert Settings().reply_interval_minutes == 5
    with pytest.raises(pydantic.ValidationError):
        Settings(reply_interval_minutes=1)
