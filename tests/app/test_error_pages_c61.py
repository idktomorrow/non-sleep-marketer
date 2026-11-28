"""대시보드 오류 응답(C61, ADR 0081): 브라우저는 오류 화면, HTMX는 조각, JSON 요청은 JSON. 상태 코드는 그대로."""

from __future__ import annotations

import pytest

from nsm.app.errors import back_link, detail_text, response_kind
from tests.app.test_card_editor import env  # noqa: F401  카드 편집 픽스처(검토 대기 원고 c1·c2)
from tests.fakes import load_comment_cases

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def _post(env, url: str, headers: dict | None = None, **data):  # noqa: F811
    return env["client"].post(
        url,
        data={"csrf": env["state"].csrf_token, **data},
        headers=headers or {},
        follow_redirects=False,
    )


def _approved(e) -> None:
    e["repo"].db.execute("UPDATE contents SET status = 'approved' WHERE id = 'c1'")


def test_browser_gets_error_page_with_status_kept(env) -> None:  # noqa: F811
    _approved(env)
    r = _post(
        env,
        "/review/c1/edit",
        headers={**HTML, "referer": "http://testserver/review/c1"},
        s0_title="x",
    )
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("text/html")
    assert "검토 대기 중인 콘텐츠만 고칠 수 있습니다" in r.text
    assert "지금은 할 수 없습니다" in r.text  # 사람 말 제목
    assert 'href="/review/c1"' in r.text  # 뒤로 = 같은 대시보드의 Referer
    assert "논슬립 마케터 v" in r.text  # base.html 레이아웃
    assert '"detail"' not in r.text


def test_htmx_gets_small_fragment(env) -> None:  # noqa: F811
    _approved(env)
    r = _post(env, "/review/c1/edit", headers={**HTML, "hx-request": "true"}, s0_title="x")
    assert r.status_code == 409 and r.headers["content-type"].startswith("text/html")
    assert r.text.startswith('<div class="notice bad" role="alert">')
    assert "검토 대기 중인" in r.text and "<html" not in r.text


def test_json_request_keeps_json(env) -> None:  # noqa: F811
    _approved(env)
    r = _post(env, "/review/c1/edit", headers={"accept": "application/json"}, s0_title="x")
    assert r.status_code == 409 and r.json() == {
        "detail": "검토 대기 중인 콘텐츠만 고칠 수 있습니다"
    }


def test_unknown_page_404_uses_same_template(env) -> None:  # noqa: F811
    r = env["client"].get("/no-such-page", headers=HTML)
    assert r.status_code == 404 and "찾을 수 없습니다" in r.text
    assert "Not Found" not in r.text  # 영어 기본 문구 대신 한국어


def test_reply_400_is_html_for_browser(env) -> None:  # noqa: F811
    comment, _ = load_comment_cases()[2]
    comment = comment.model_copy(update={"platform": "threads"})
    env["repo"].insert_comment(comment)
    key = env["repo"].comment_key(comment)
    env["repo"].update_comment(key, status="escalated", reason="불만")
    r = _post(env, f"/comments/{key}/reply", headers=HTML, text="   ")
    assert r.status_code == 400 and "답글 내용이 비어 있습니다" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_edit_400_rerenders_form_with_sent_values(env) -> None:  # noqa: F811
    deck = env["repo"].get_content("c2").generated.deck
    li = next(i for i, s in enumerate(deck.slides) if s.kind == "list")
    r = _post(
        env,
        "/review/c2/edit",
        headers=HTML,
        instagram_caption="내가 방금 고친 캡션 <b>굵게</b>",
        **{f"s{li}_bullets": "\n  \n"},  # 목록은 1개 이상 → 400
    )
    assert r.status_code == 400
    assert 'id="edit-error"' in r.text and f"{li + 1}장" in r.text
    # 보낸 캡션이 그대로 칸에 남는다(이스케이프되어)
    assert "내가 방금 고친 캡션 &lt;b&gt;굵게&lt;/b&gt;</textarea>" in r.text
    assert env["repo"].get_content("c2").generated.deck == deck  # 아무것도 저장 안 됨


def test_query_typos_fall_back_to_defaults(env) -> None:  # noqa: F811
    assert env["client"].get("/settings?saved=x").status_code == 200
    assert "저장했습니다" not in env["client"].get("/settings?saved=x").text
    assert "저장했습니다" in env["client"].get("/settings?saved=1").text
    r = env["client"].get("/logs?all=x")
    assert r.status_code == 200 and "모든 실행" not in r.text


@pytest.mark.parametrize(
    ("headers", "kind"),
    [
        ({"hx-request": "true", "accept": "application/json"}, "htmx"),
        ({"accept": "application/json"}, "json"),
        ({"accept": "text/html"}, "html"),
        ({"accept": "*/*"}, "json"),
        ({}, "json"),
    ],
)
def test_response_kind(headers, kind) -> None:
    from starlette.requests import Request

    raw = [(k.encode(), v.encode()) for k, v in headers.items()]
    req = Request({"type": "http", "headers": raw, "method": "GET", "path": "/"})
    assert response_kind(req) == kind


def test_back_link_only_same_dashboard() -> None:
    from starlette.requests import Request

    def req(ref: str) -> Request:
        h = [(b"host", b"127.0.0.1:8765")] + ([(b"referer", ref.encode())] if ref else [])
        return Request(
            {
                "type": "http",
                "headers": h,
                "method": "GET",
                "path": "/x",
                "scheme": "http",
                "server": ("127.0.0.1", 8765),
                "query_string": b"",
            }
        )

    assert back_link(req("http://127.0.0.1:8765/review?x=1")) == "/review?x=1"
    assert back_link(req("https://evil.example/review")) == "/"
    assert back_link(req("javascript:alert(1)")) == "/"
    assert back_link(req("")) == "/"
    assert detail_text(404, "Not Found").startswith("주소가")


def test_settings_short_window_shown_in_form(env) -> None:  # noqa: F811
    from nsm.app.settings_form import form_values

    form = {**form_values(env["state"].get_config()), "active_hours": "12:00-12:30"}
    r = _post(env, "/settings", headers=HTML, **form)
    assert r.status_code == 400 and "활동 시간 창은 60분 이상이어야 합니다" in r.text
    assert 'value="12:00-12:30"' in r.text or ">12:00-12:30<" in r.text  # 넣은 값이 남는다
