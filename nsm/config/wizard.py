"""`nsm init` 설정 마법사.

입출력을 `WizardIO`로 감싸서, 테스트에서는 미리 정한 답으로 끝까지 돌릴 수 있다.
이미 설정이 있으면 기존 값을 기본값으로 보여줘 "다시 고치기"가 된다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nsm.config.paths import config_path, uploads_dir
from nsm.config.schema import CARD_LOOK_FIELDS, AppConfig, BrandSettings, Settings
from nsm.config.secrets import KEY_NAMES, KEY_USES, KEYS, KeyringSecretStore, SecretStore, mask
from nsm.config.store import (
    ConfigNotFoundError,
    format_windows,
    load_config,
    parse_windows,
    save_config,
    split_list,
)
from nsm.domain.labels import DEFAULT_BANNED_PATTERNS
from nsm.domain.models import Persona, _check_hex

RISK_NOTICE = """\
[위험 고지]
- 인스타그램·스레드·네이버·커뮤니티 약관은 자동화 도구 사용을 제한합니다.
  이 도구를 쓰면 계정이 제한되거나 정지될 수 있습니다.
- 빈도를 낮추고 무작위 간격을 두지만 위험을 없애지는 못합니다.
- 메인 계정 대신 테스트 계정으로 먼저 써 보고, 수집에는 수집 전용 계정을 쓰세요.
- 설치 직후에는 보호 모드로 첫 20건을 모두 사람이 검토합니다.
- 모든 게시물에 'AI 가상인물' 표기가 자동으로 들어가며 끌 수 없습니다.
"""

KEY_LABELS = {
    k: f"{KEY_NAMES[k]} API 키 ({KEY_USES[k]})" for k in KEY_NAMES
}  # 설정 화면과 같은 설명


class WizardAbortedError(Exception):
    pass


class WizardIO(Protocol):
    def echo(self, text: str) -> None: ...
    def prompt(self, text: str, default: str | None = None, hide_input: bool = False) -> str: ...
    def confirm(self, text: str, default: bool = False) -> bool: ...


class TyperIO:
    def echo(self, text: str) -> None:
        import typer

        typer.echo(text)

    def prompt(self, text: str, default: str | None = None, hide_input: bool = False) -> str:
        import typer

        return str(
            typer.prompt(
                text,
                default=default if default is not None else "",
                hide_input=hide_input,
                show_default=not hide_input,
            )
        )

    def confirm(self, text: str, default: bool = False) -> bool:
        import typer

        return bool(typer.confirm(text, default=default))


def _ask_color(io: WizardIO, text: str, default: str) -> str:
    while True:
        v = _ask(io, text, default)
        try:
            return _check_hex(v)
        except ValueError as e:
            io.echo(f"  {e}")


def _ask_list(io: WizardIO, text: str, default: str) -> list[str]:
    while True:
        items = split_list(_ask(io, text, default))
        if items:
            return items
        io.echo("  하나 이상 입력해 주세요 (쉼표로 구분).")


def _ask(io: WizardIO, text: str, default: str = "", required: bool = True) -> str:
    while True:
        v = io.prompt(text, default=default).strip()
        if v or not required:
            return v
        io.echo("  값을 입력해 주세요.")


def wizard(
    io: WizardIO,
    secrets: SecretStore,
    path: Path | None = None,
    now: datetime | None = None,
) -> AppConfig:
    path = path or config_path()
    try:
        existing: AppConfig | None = load_config(path)
        io.echo(f"기존 설정을 불러왔습니다: {path}\n엔터를 누르면 기존 값을 유지합니다.\n")
    except ConfigNotFoundError:
        existing = None
    except Exception as e:  # noqa: BLE001  깨진 설정도 마법사로 고칠 수 있어야 한다
        backup = path.with_suffix(".yaml.broken")
        path.replace(backup)
        io.echo(f"기존 설정을 읽지 못해 {backup.name}로 옮기고 새로 만듭니다: {e}\n")
        existing = None

    # 1. 위험 고지
    io.echo(RISK_NOTICE)
    if not io.confirm("위 내용을 이해했고 동의합니다", default=False):
        raise WizardAbortedError("위험 고지에 동의하지 않아 설정을 중단했습니다.")
    accepted_at = now or datetime.now(UTC)

    # 2. 브랜드
    io.echo("\n[1/4] 브랜드")
    b = existing.brand if existing else None
    brand = BrandSettings(
        name=_ask(io, "브랜드 이름", b.name if b else ""),
        industry=_ask(io, "업종", b.industry if b else ""),
        products=split_list(
            _ask(io, "대표 제품 (쉼표로 구분)", ", ".join(b.products) if b else "", required=False)
        ),
        key_messages=split_list(
            _ask(
                io,
                "핵심 메시지 (쉼표로 구분)",
                ", ".join(b.key_messages) if b else "",
                required=False,
            )
        ),
        primary_color=_ask_color(io, "브랜드 주 색 (#RRGGBB)", b.primary_color if b else "#1E40AF"),
        accent_color=_ask_color(io, "강조 색 (#RRGGBB)", b.accent_color if b else "#F59E0B"),
        logo_path=b.logo_path if b else None,
        # 대시보드에서 고른 카드 스타일·캐릭터를 지키기
        **({k: getattr(b, k) for k in CARD_LOOK_FIELDS} if b else {}),
    )

    # 3. 페르소나
    io.echo("\n[2/4] AI 가상 인플루언서 페르소나")
    p = existing.persona if existing else None
    while True:
        hours_text = _ask(
            io,
            "활동 시간 (예: 09:00-12:00, 18:00-23:00)",
            format_windows(p.active_hours) if p else "09:00-12:00, 18:00-23:00",
        )
        try:
            windows = parse_windows(hours_text)
            break
        except ValueError as e:
            io.echo(f"  {e}")
    user_patterns = [
        x for x in (p.banned_patterns if p else []) if x not in DEFAULT_BANNED_PATTERNS
    ]
    persona = Persona(
        name=_ask(io, "페르소나 이름", p.name if p else ""),
        handle=_ask(io, "계정 핸들 (예: @luna.ai)", p.handle if p else ""),
        character=_ask(io, "인물상 (나이·직업·성격을 한 문장으로)", p.character if p else ""),
        tone=_ask(io, "말투", p.tone if p else "다정한 존댓말, 짧은 문장"),
        topics=_ask_list(io, "관심 주제 (쉼표로 구분)", ", ".join(p.topics) if p else ""),
        banned_words=split_list(
            _ask(io, "금칙어 (쉼표로 구분)", ", ".join(p.banned_words) if p else "", required=False)
        ),
        banned_patterns=[*DEFAULT_BANNED_PATTERNS, *user_patterns],
        active_hours=windows,
        ad_label=io.confirm("협찬(광고) 게시물로 표기할까요?", default=p.ad_label if p else False),
        enabled=io.confirm(
            "설정이 끝나면 페르소나를 바로 켤까요?", default=p.enabled if p else True
        ),
    )

    # 4. API 키
    io.echo("\n[3/4] API 키 (Windows 자격 증명 관리자에 저장, 엔터를 누르면 건너뜀)")
    new_keys: set[str] = set()
    for key in KEYS:
        current = secrets.get(key)
        v = io.prompt(
            f"{KEY_LABELS[key]} [현재: {mask(current)}]", default="", hide_input=True
        ).strip()
        if v:
            try:
                secrets.set(key, v)
                new_keys.add(key)
            except Exception as e:  # noqa: BLE001  키 저장 실패가 나머지 설정을 날리면 안 된다
                io.echo(
                    f"  키를 저장하지 못했습니다({e}). 환경 변수로 넣을 수 있습니다: "
                    "사용설명서 2절 참고."
                )

    # 5. 플랫폼
    io.echo("\n[4/4] 플랫폼 계정")
    s = existing.settings if existing else Settings()
    dry_run = io.confirm(
        "지금은 계정 연결 없이 드라이런(미리보기 저장)으로 시작할까요?", default=s.dry_run
    )
    settings = s.model_copy(update={"dry_run": dry_run})
    # AI 배경 이미지는 유료라 기본으로 꺼 둔다(ADR 0036). OpenAI 키를 넣을 때만 켤지 묻는다
    ask_images = "openai_api_key" in new_keys and settings.image_generation == "off"
    if ask_images and io.confirm(
        "OpenAI 키를 넣었습니다. AI 배경 이미지를 켤까요? (이미지마다 OpenAI 요금이 듭니다)",
        default=True,
    ):
        settings = settings.model_copy(update={"image_generation": "auto"})
    if not dry_run:
        io.echo("  실제 계정 로그인은 `nsm run` 후 브라우저 창에서 진행합니다(사용설명서 5절).")

    cfg = AppConfig(risk_accepted_at=accepted_at, brand=brand, persona=persona, settings=settings)
    save_config(cfg, path)
    uploads_dir().mkdir(parents=True, exist_ok=True)
    io.echo(f"\n설정을 저장했습니다: {path}")
    io.echo(
        f"직접 올릴 자료(제품 정보, 보도자료, 엑셀, 고객 후기)는 여기에 넣으세요: {uploads_dir()}"
    )
    io.echo("다음: `nsm doctor`로 점검한 뒤 `nsm run`")
    return cfg


def run_wizard() -> None:
    try:
        wizard(TyperIO(), KeyringSecretStore())
    except WizardAbortedError as e:
        raise SystemExit(str(e)) from e
