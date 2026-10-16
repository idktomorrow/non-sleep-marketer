"""사용자 데이터 위치. 환경 변수 NSM_HOME으로 바꿀 수 있다(테스트·여러 브랜드 운영)."""

from __future__ import annotations

import os
from pathlib import Path


def nsm_home() -> Path:
    env = os.environ.get("NSM_HOME")
    return Path(env) if env else Path.home() / ".nsm"


def config_path() -> Path:
    return nsm_home() / "config.yaml"


def db_path() -> Path:
    return nsm_home() / "data.duckdb"


def preview_dir() -> Path:
    return nsm_home() / "preview"


def uploads_dir() -> Path:
    return nsm_home() / "uploads"


def media_dir() -> Path:
    return nsm_home() / "media"


def reports_dir() -> Path:
    return nsm_home() / "reports"


def backups_dir() -> Path:
    return nsm_home() / "backups"


def logs_dir() -> Path:
    return nsm_home() / "logs"
