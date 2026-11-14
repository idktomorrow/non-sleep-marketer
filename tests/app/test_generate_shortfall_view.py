"""C46: 쓸 수 있는 자료가 0일 때 다음 수집 시각 안내, 자료 탭 offset이 숫자가 아닐 때."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nsm.app.server import next_collect_text
from nsm.engine.catch_up import Slot, next_daily
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처
from tests.app.test_materials import menv  # noqa: F401

KST = ZoneInfo("Asia/Seoul")


def _card(html: str) -> str:
    i = html.index("쓸 수 있는 자료")
    return html[i : html.index("</div>", i)]


def _no_material(env) -> None:  # noqa: F811
    env["repo"].db.execute("UPDATE source_items SET used_in_content_id = 'x'")


def test_zero_material_card_says_next_collect_today(env, clock) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    _no_material(env)
    card = _card(env["client"].get("/").text)  # 10:00, 수집은 09:20·18:20(활동 창 시작 20분 뒤)
    assert "<b>0</b>" in card and "다음 수집 18:20에 다시 모읍니다" in card
    assert "원고를 만들 때 씁니다" not in card


def test_zero_material_card_says_tomorrow_after_last_slot(env, clock) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    _no_material(env)
    clock.set(datetime(2026, 10, 11, 19, 0, tzinfo=KST))
    assert "다음 수집 내일 09:20에 다시 모읍니다" in _card(env["client"].get("/").text)


def test_card_keeps_old_text_when_material_left(env) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    env["repo"].db.execute(
        "UPDATE source_items SET relevance = 0.9, text = repeat('가', 500), facts = NULL"
    )
    card = _card(env["client"].get("/").text)
    assert "<b>0</b>" not in card
    assert "원고를 만들 때 씁니다" in card and "다음 수집" not in card


def test_next_daily_and_fallback(app_config) -> None:
    at = datetime(2026, 11, 15, 13, 0, tzinfo=KST)
    one = [Slot(0, 20, 1800)]  # 운영 설정: 하루 1회 00:20
    assert next_daily(one, at, "Asia/Seoul") == (one[0], True)
    assert next_daily(one, datetime(2026, 11, 15, 0, 10, tzinfo=KST), "Asia/Seoul") == (
        one[0],
        False,
    )
    assert next_daily([], at, "Asia/Seoul") is None
    bad = app_config.model_copy(
        update={"persona": app_config.persona.model_copy(update={"active_hours": []})}
    )
    assert next_collect_text(bad, at, "Asia/Seoul") is None  # 못 구하면 원래 문구
    off = app_config.model_copy(
        update={"persona": app_config.persona.model_copy(update={"enabled": False})}
    )
    assert (
        next_collect_text(off, at, "Asia/Seoul") is None
    )  # 수집이 돌지 않으니 시각을 말하지 않는다
    assert next_collect_text(app_config, at, "Asia/Seoul") == "다음 수집 18:20에 다시 모읍니다"


@pytest.mark.parametrize("raw", ["abc", "", "1.5", "--5", "²"])
def test_materials_non_numeric_offset_is_first_page(menv, raw) -> None:  # noqa: F811
    r = menv["client"].get(f"/materials?offset={raw}")
    first = menv["client"].get("/materials")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert r.text.count("<tr") == first.text.count("<tr")  # 첫 쪽과 같은 행 수
    assert "이전 50건" not in r.text


def test_materials_negative_offset(menv) -> None:  # noqa: F811
    r = menv["client"].get("/materials?offset=-5")
    assert r.status_code == 200 and "이전 50건" not in r.text
