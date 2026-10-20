"""프리셋(C29, ADR 0049): 페르소나 + 브랜드를 이름 붙여 저장하고 갈아끼운다.

프리셋은 "설정 묶음"이 아니라 **작업 공간**이다. 자료·원고·댓글·성과는 페르소나에 속하므로(관련성 판정이
관심 주제 기준이라 섞이면 다른 프리셋이 자료를 못 받는다) 프리셋마다 DB 파일을 따로 둔다.

폴더: <NSM_HOME>/presets/<slug>/
  preset.yaml   페르소나(계정 핸들 제외) + 브랜드
  brand/        프리셋에 딸린 캐릭터 그림 복사본
  data.duckdb   그 프리셋의 자료·원고·댓글·성과 (기본 프리셋은 기존 <NSM_HOME>/data.duckdb 그대로)

계정·브라우저 로그인·API 키·운영 설정(settings)은 프리셋과 무관하게 공유한다.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

from nsm.config import paths
from nsm.config.schema import AppConfig, BrandSettings
from nsm.domain.models import Persona

DEFAULT_PRESET = "기본"
PRESET_FILE = "preset.yaml"
_IMAGE_FIELDS = ("character_cover", "character_outro")
_MAX_NAME = 30


class PresetError(ValueError):
    pass


@dataclass(frozen=True)
class PresetInfo:
    name: str
    slug: str
    persona_name: str
    brand_name: str
    topics: tuple[str, ...]


def check_name(name: str) -> str:
    """이름 규칙. NFC로 정규화한 이름을 돌려준다(같은 글자를 다르게 조합한 입력이 다른 이름이 되지 않게)."""
    name = unicodedata.normalize("NFC", name.strip())
    if not name:
        raise PresetError("프리셋 이름을 넣어 주세요")
    if len(name) > _MAX_NAME:
        raise PresetError(f"프리셋 이름은 {_MAX_NAME}자까지입니다")
    if any(ch in name for ch in '\\/:*?"<>|'):
        raise PresetError('프리셋 이름에 \\ / : * ? " < > | 는 쓸 수 없습니다')
    return name


def slugify(name: str) -> str:
    """폴더 이름. 한글은 그대로 두고 경로에 못 쓰는 글자·공백만 바꾼다. 서로 다른 이름이 같은 폴더가 될 수 있어
    (예: "내 브랜드"와 "내-브랜드") 비교는 항상 slug끼리 한다."""
    s = unicodedata.normalize("NFC", name.strip())
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-.")
    if not s:
        raise PresetError("프리셋 이름이 비어 있습니다")
    return s[:_MAX_NAME]


def same(a: str, b: str) -> bool:
    """두 이름이 같은 프리셋(같은 폴더)을 가리키나."""
    try:
        return slugify(a) == slugify(b)
    except PresetError:
        return False


def workspace_db_path(cfg: AppConfig, store: PresetStore | None = None) -> Path:
    """설정이 가리키는 프리셋의 DB 경로. 켤 때 이 경로를 열어야 전환한 프리셋이 재시작 뒤에도 이어진다."""
    return (store or PresetStore()).db_path(cfg.settings.preset)


class PresetStore:
    def __init__(self, root: Path | None = None, default_db: Path | None = None) -> None:
        self.root = root or (paths.nsm_home() / "presets")
        self.default_db = default_db or paths.db_path()

    # ---------------------------------------------------------------- 경로
    def dir(self, name: str) -> Path:
        return self.root / slugify(name)

    def db_path(self, name: str) -> Path:
        """기본 프리셋은 기존 DB 그대로(옮기지 않는다). 나머지는 프리셋 폴더 안."""
        if same(name, DEFAULT_PRESET):
            return self.default_db
        return self.dir(name) / "data.duckdb"

    def exists(self, name: str) -> bool:
        try:
            return (self.dir(name) / PRESET_FILE).is_file()
        except PresetError:
            return False

    def stored_name(self, name: str) -> str | None:
        """그 폴더에 저장된 프리셋의 원래 이름(없으면 None)."""
        f = self.dir(name) / PRESET_FILE
        if not f.is_file():
            return None
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        return str(raw.get("name") or f.parent.name)

    # ---------------------------------------------------------------- 읽기
    def list(self) -> list[PresetInfo]:
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            f = d / PRESET_FILE
            if not f.is_file():
                continue
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                p = raw.get("persona") or {}
                b = raw.get("brand") or {}
                out.append(
                    PresetInfo(
                        name=str(raw.get("name") or d.name),
                        slug=d.name,
                        persona_name=str(p.get("name", "")),
                        brand_name=str(b.get("name", "")),
                        topics=tuple(str(t) for t in (p.get("topics") or [])),
                    )
                )
            except Exception:  # noqa: BLE001  깨진 파일 하나가 목록 전체를 막지 않게
                continue
        # 기본 프리셋이 먼저
        out.sort(key=lambda i: (not same(i.name, DEFAULT_PRESET), i.name))
        return out

    def load_into(self, cfg: AppConfig, name: str) -> AppConfig:
        """프리셋의 페르소나·브랜드를 cfg에 입힌 새 설정. 계정 핸들은 지금 것을 지킨다(실계정과 묶여 있다)."""
        f = self.dir(name) / PRESET_FILE
        if not f.is_file():
            raise PresetError(f"프리셋 '{name}'이 없습니다")
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        persona = Persona.model_validate(
            {**(raw.get("persona") or {}), "handle": cfg.persona.handle}
        )
        brand = BrandSettings.model_validate(raw.get("brand") or {})
        stored = str(raw.get("name") or name)
        settings = cfg.settings.model_copy(update={"preset": stored})
        return cfg.model_copy(update={"persona": persona, "brand": brand, "settings": settings})

    # ---------------------------------------------------------------- 쓰기
    def save(self, cfg: AppConfig, name: str, overwrite: bool = True) -> Path:
        """지금 설정의 페르소나·브랜드를 프리셋으로 저장. 캐릭터 그림은 프리셋 폴더로 복사해 둔다.

        overwrite=False면 이미 있는 프리셋(같은 폴더가 되는 다른 이름 포함)은 덮어쓰지 않는다.
        """
        name = check_name(name)
        existing = self.stored_name(name)
        if existing is not None and existing != name:
            raise PresetError(
                f"'{name}'은 이미 있는 프리셋 '{existing}'과 같은 폴더가 됩니다. 다른 이름을 쓰세요"
            )
        if existing is not None and not overwrite:
            raise PresetError(
                f"'{name}' 프리셋이 이미 있습니다. 덮어쓰려면 그 프리셋으로 전환한 뒤 저장하세요"
            )
        d = self.dir(name)
        d.mkdir(parents=True, exist_ok=True)
        brand = cfg.brand.model_dump(mode="json", exclude_none=True)
        for field in _IMAGE_FIELDS:
            src = brand.get(field)
            if not src:
                continue
            sp = Path(src)
            if not sp.is_file():
                brand.pop(field, None)
                continue
            dst = d / "brand" / f"{field}{sp.suffix.lower()}"
            if sp.resolve() != dst.resolve():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sp, dst)
            brand[field] = str(dst)
        persona = cfg.persona.model_dump(mode="json", exclude_none=True)
        persona.pop("handle", None)  # 계정은 프리셋과 무관
        data = {"name": name, "persona": persona, "brand": brand}
        f = d / PRESET_FILE
        tmp = f.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(f)
        return f

    def delete(self, name: str, current: str) -> None:
        """프리셋 폴더를 통째로 지운다(DB 포함). 지금 쓰는 프리셋과 기본 프리셋은 못 지운다. 비교는 폴더 기준."""
        if same(name, current):
            raise PresetError("지금 쓰는 프리셋은 지울 수 없습니다. 먼저 다른 프리셋으로 바꾸세요")
        if same(name, DEFAULT_PRESET):
            raise PresetError("기본 프리셋은 지울 수 없습니다")
        d = self.dir(name)
        if not (d / PRESET_FILE).is_file():
            raise PresetError(f"프리셋 '{name}'이 없습니다")
        shutil.rmtree(d)
