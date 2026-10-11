from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from nsm.config.schema import AppConfig
from nsm.store.db import Database
from tests.fakes import FIXTURES, FixedClock

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def clock() -> FixedClock:
    # 활동 시간(09~12시) 안의 일요일 오전
    return FixedClock(datetime(2026, 10, 11, 10, 0, tzinfo=KST))


@pytest.fixture
def app_config() -> AppConfig:
    raw = yaml.safe_load((FIXTURES / "personas" / "sample.yaml").read_text("utf-8"))
    return AppConfig.model_validate(raw)


@pytest.fixture
def nsm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "nsm-home"
    monkeypatch.setenv("NSM_HOME", str(home))
    return home
