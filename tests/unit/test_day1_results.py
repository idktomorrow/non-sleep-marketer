"""배치 B(1일차 검증) 결과를 코드에 반영한 것들. 근거: docs/day1-verification.md."""

from __future__ import annotations

import re
from pathlib import Path

from nsm import doctor
from nsm.adapters import drivers
from nsm.adapters.drivers.playwright_driver import PlaywrightDriver
from nsm.adapters.platforms import PLATFORM_CAPABILITIES
from nsm.config.schema import Settings
from nsm.domain.labels import AD_HASHTAG, VIRTUAL_PERSON_TEXT
from nsm.domain.policy import apply_labels


def test_virtual_person_text_is_ftc_guideline_wording() -> None:
    # 공정위 예규 제499호 Ⅴ.5 예시 문구 그대로 (ADR 0019)
    assert VIRTUAL_PERSON_TEXT == "인공지능(AI)을 기반으로 생성된 가상인물이 포함된 게시물입니다"


def test_ad_hashtag_is_first_hashtag_before_body_hashtags() -> None:
    # 지침 Ⅴ.6: 해시태그로 표시하면 첫 번째 해시태그, "여러 해시태그 사이"는 부적절
    text = apply_labels("본문입니다\n\n#핸드드립 #홈카페", is_ad=True)
    assert re.findall(r"#\S+", text)[0] == AD_HASHTAG
    assert text.splitlines()[0].startswith(AD_HASHTAG)


def test_hide_capability_matches_day1_verification() -> None:
    assert "hide_comment" in PLATFORM_CAPABILITIES["threads"]
    assert "hide_comment" not in PLATFORM_CAPABILITIES["instagram"]
    # 숨기기 말고는 두 플랫폼 모두 같은 일을 한다
    assert PLATFORM_CAPABILITIES["threads"] - {"hide_comment"} == PLATFORM_CAPABILITIES["instagram"]


def test_settings_defaults_after_day1() -> None:
    s = Settings()
    assert s.browser_driver == "aside"  # ADR 0017
    assert s.accounts == {}  # 비어 있으면 실계정 게시 안 함
    assert s.classifier == "auto"  # MotherDuck 토큰이 있으면 jev


def test_aside_driver_falls_back_to_playwright_until_batch_g(tmp_path: Path) -> None:
    d = drivers.make_driver("aside", tmp_path / "profile", headless=True)
    assert isinstance(d, PlaywrightDriver)  # 브라우저는 첫 사용 때 뜬다(여기선 안 뜸)
    assert not (tmp_path / "profile").exists()


def test_find_aside_cli_uses_localappdata(tmp_path: Path, monkeypatch) -> None:
    exe = tmp_path / "Aside" / "CLI" / "current" / "aside.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(drivers.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert drivers.find_aside_cli() == exe
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
    assert drivers.find_aside_cli() is None


def test_doctor_aside_check(monkeypatch, nsm_home) -> None:
    monkeypatch.setattr(drivers, "find_aside_cli", lambda: None)
    assert doctor._aside().status == "WARN"
    monkeypatch.setattr(drivers, "find_aside_cli", lambda: Path("C:/aside.exe"))
    c = doctor._aside()
    assert c.status == "OK" and "로그인" in c.detail
