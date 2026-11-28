"""config.yaml 읽기·쓰기와 활동 시간 문자열 변환."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import ValidationError

from nsm.config.paths import config_path
from nsm.config.schema import MIN_WINDOW_MINUTES, AppConfig, check_windows
from nsm.domain.models import ActiveWindow


class ConfigNotFoundError(FileNotFoundError):
    pass


class ConfigInvalidError(ValueError):
    """config.yaml을 읽었지만 형식이 틀림(C61). CLI가 트레이스 없이 한 줄로 보여 준다."""


def load_config(path: Path | None = None) -> AppConfig:
    p = path or config_path()
    if not p.exists():
        raise ConfigNotFoundError(f"설정 파일이 없습니다: {p}. `nsm init`을 먼저 실행하세요.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return AppConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise ConfigInvalidError(
            f"설정 파일이 잘못됐습니다: {p}. {_first_error(e)}. "
            "`nsm init`으로 다시 만들거나 그 줄을 고친 뒤 다시 실행하세요."
        ) from e


def _first_error(e: Exception) -> str:
    if isinstance(e, ValidationError) and e.errors():
        err = e.errors()[0]
        where = ".".join(str(x) for x in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        return f"{where}: {msg}" if where else msg
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json", exclude_none=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(p)  # 쓰다가 죽어도 기존 파일이 깨지지 않게
    return p


def parse_windows(text: str, min_minutes: int = MIN_WINDOW_MINUTES) -> list[ActiveWindow]:
    """'09:00-12:00, 18:00-23:00' → [ActiveWindow, ...] (시작 시각 순, 창마다 min_minutes 이상, C61)"""
    windows: list[ActiveWindow] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a, b = (s.strip() for s in part.split("-"))
            windows.append(ActiveWindow(start=time.fromisoformat(a), end=time.fromisoformat(b)))
        except ValueError as e:
            raise ValueError(f"활동 시간 형식이 잘못됐습니다: '{part}' (예: 09:00-12:00)") from e
    if not windows:
        raise ValueError("활동 시간이 하나 이상 필요합니다")
    return check_windows(windows, min_minutes)


def format_windows(windows: list[ActiveWindow]) -> str:
    return ", ".join(f"{w.start:%H:%M}-{w.end:%H:%M}" for w in windows)


def split_list(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]
