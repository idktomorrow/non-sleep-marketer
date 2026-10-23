"""대시보드의 키워드 원고(C15)와 수집 제외어 설정."""

from __future__ import annotations

from concurrent.futures import Future

from nsm.app.settings_form import apply_settings_form, form_values
from nsm.engine.context import JobResult
from tests.app.test_dashboard import env  # noqa: F401  같은 대시보드 픽스처


def _post(env, keyword: str, csrf: str | None = None):  # noqa: F811
    return env["client"].post(
        "/keyword-draft",
        data={"csrf": env["state"].csrf_token if csrf is None else csrf, "keyword": keyword},
        headers={"referer": "http://testserver/"},
        follow_redirects=False,
    )


def _wire(e) -> list[str]:
    asked: list[str] = []

    def run(kw: str) -> Future:
        asked.append(kw)
        f: Future = Future()
        r = JobResult("keyword", counts={"keyword": kw, "created": 1, "pending_review": 1})
        f.set_result(r)
        return f

    e["state"].run_keyword_now = run
    return asked


def test_home_shows_keyword_box_in_both_modes(env) -> None:  # noqa: F811
    assert 'action="/keyword-draft"' not in env["client"].get("/").text  # 스케줄러 없음
    _wire(env)
    assert 'action="/keyword-draft"' in env["client"].get("/").text  # 간단 모드
    env["repo"].set_setting("ui_mode", "advanced")
    assert 'action="/keyword-draft"' in env["client"].get("/").text


def test_keyword_post_starts_the_job_and_shows_progress(env) -> None:  # noqa: F811
    asked = _wire(env)
    r = _post(env, "  치이카와  굿즈 ")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert asked == ["info|치이카와 굿즈"]  # 종류(정보·밈·둘 다)를 함께 넘긴다(기본 정보)
    html = env["client"].get("/jobs/status").text
    assert "키워드 원고 &#39;치이카와 굿즈&#39; 완료" in html  # 진행 표시에 키워드도 보인다
    assert "만듦" in html


def test_bad_keyword_is_explained_and_not_run(env) -> None:  # noqa: F811
    asked = _wire(env)
    r = _post(env, "a")
    assert r.status_code == 303 and "kw_error=" in r.headers["location"] and asked == []
    assert "2자 이상" in env["client"].get(r.headers["location"]).text
    assert _post(env, "치이카와 굿즈", csrf="wrong").status_code == 403
    assert asked == []


def test_keyword_without_scheduler_is_503(env) -> None:  # noqa: F811
    assert _post(env, "치이카와 굿즈").status_code == 503


def test_settings_exclude_words_round_trip(env) -> None:  # noqa: F811
    cfg = env["state"].get_config()
    new = apply_settings_form(cfg, {**form_values(cfg), "exclude_words": "극장판\n 극장판 \n관객"})
    assert new.settings.collect.exclude_words == ["극장판", "관객"]
    assert form_values(new)["exclude_words"] == "극장판\n관객"
    # 칸이 없는 옛 폼은 지금 값을 지킨다
    form = form_values(new)
    del form["exclude_words"]
    assert apply_settings_form(new, form).settings.collect.exclude_words == ["극장판", "관객"]
    assert 'name="exclude_words"' in env["client"].get("/settings").text  # 간단 모드에서도 보임


def test_same_keyword_twice_while_running_starts_once(env) -> None:  # noqa: F811
    asked: list[str] = []
    pending: Future = Future()

    def run(kw: str) -> Future:
        asked.append(kw)
        return pending

    env["state"].run_keyword_now = run
    _post(env, "치이카와 굿즈")
    _post(env, "치이카와  굿즈")  # 두 번 누름(공백만 다름)
    _post(env, "먼작귀 굿즈")  # 다른 키워드는 따로
    assert asked == ["info|치이카와 굿즈", "info|먼작귀 굿즈"]
    pending.set_result(JobResult("keyword", counts={"keyword": "치이카와 굿즈"}))
    _post(env, "치이카와 굿즈")  # 끝난 뒤에는 다시 할 수 있다
    assert asked[-1] == "info|치이카와 굿즈" and len(asked) == 3


def test_keyword_kind_choice_is_passed_and_validated(env) -> None:  # noqa: F811
    """정보·밈·둘 다(S18): 고른 종류를 같은 흐름에 넘긴다. 다른 값은 거절."""
    asked = _wire(env)
    env["client"].post(
        "/keyword-draft",
        data={"csrf": env["state"].csrf_token, "keyword": "치이카와", "kind": "both"},
        follow_redirects=False,
    )
    assert asked == ["both|치이카와"]
    r = env["client"].post(
        "/keyword-draft",
        data={"csrf": env["state"].csrf_token, "keyword": "치이카와", "kind": "video"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert 'name="kind" value="meme"' in env["client"].get("/").text
