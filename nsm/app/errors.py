"""대시보드 오류 응답(C61, ADR 0081). 폼을 보내다 400·409가 나면 브라우저에 `{"detail": …}` JSON이 떴다.

요청 종류에 따라 같은 오류를 세 가지로 돌려준다. 상태 코드는 그대로다(테스트·스크립트가 400·409를 본다).
- HTMX 요청(`HX-Request` 헤더): detail 문구만 든 작은 배너 조각(HTML)
- Accept에 application/json: 지금처럼 `{"detail": …}` JSON
- Accept에 text/html(브라우저 폼·주소창): base.html 레이아웃의 짧은 오류 화면과 "뒤로" 링크
- 그 밖(Accept 없음·`*/*`, 예: 테스트 클라이언트·fetch): JSON. 스크립트가 기대하던 모양을 지킨다
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from html import escape
from http import HTTPStatus
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

log = logging.getLogger(__name__)

TITLES = {
    400: "입력을 확인해 주세요",
    403: "화면이 오래됐습니다",
    404: "찾을 수 없습니다",
    409: "지금은 할 수 없습니다",
    500: "처리하다 오류가 났습니다",
    502: "플랫폼에 보내지 못했습니다",
    503: "지금은 쓸 수 없습니다",
    504: "시간이 너무 걸렸습니다",
}
# detail 없이 올린 예외는 영어 기본 문구("Not Found")가 들어 있다. 사람 말로 바꾼다
DEFAULT_DETAILS = {
    403: "화면을 새로고침한 뒤 다시 시도하세요",
    404: "주소가 틀렸거나 이미 지워진 항목입니다",
    405: "이 주소는 그 방식으로 열 수 없습니다",
}

Kind = Literal["htmx", "json", "html"]


def response_kind(request: Request) -> Kind:
    if request.headers.get("hx-request"):
        return "htmx"
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return "json"
    if "text/html" in accept:
        return "html"
    return "json"


def wants_html(request: Request) -> bool:
    """폼을 다시 그려 줄지(편집 화면 400 등): 브라우저 폼 요청일 때만."""
    return response_kind(request) == "html"


def back_link(request: Request) -> str:
    """같은 대시보드 안의 Referer만 "뒤로"로 쓴다. 다른 곳·없으면 첫 화면."""
    ref = request.headers.get("referer", "")
    if not ref:
        return "/"
    u = urlsplit(ref)
    if u.scheme not in ("http", "https") or u.netloc != request.url.netloc:
        return "/"
    return (u.path or "/") + (f"?{u.query}" if u.query else "")


def detail_text(status: int, detail: Any) -> str:
    text = detail if isinstance(detail, str) else str(detail)
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = ""
    if not text or text == phrase:
        return DEFAULT_DETAILS.get(status, "요청을 처리하지 못했습니다")
    return text


def banner(detail: str) -> str:
    return f'<div class="notice bad" role="alert">{escape(detail)}</div>'


def title_for(status: int) -> str:
    if status in TITLES:
        return TITLES[status]
    return "처리하다 오류가 났습니다" if status >= 500 else "요청을 처리하지 못했습니다"


def install_error_handlers(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        status = exc.status_code
        headers = getattr(exc, "headers", None)
        kind = response_kind(request)
        if kind == "json":
            return JSONResponse({"detail": exc.detail}, status_code=status, headers=headers)
        detail = detail_text(status, exc.detail)
        if kind == "htmx":
            return HTMLResponse(banner(detail), status_code=status, headers=headers)
        try:
            resp = await run_in_threadpool(
                page,
                request,
                "error.html",
                error_title=title_for(status),
                error_detail=detail,
                error_status=status,
                error_back=back_link(request),
            )
        except Exception:  # noqa: BLE001  화면 틀을 못 그려도(DB 오류 등) 문구는 보여 준다
            log.exception("오류 화면을 그리지 못함")
            body = (
                f"<!doctype html><meta charset='utf-8'><title>{escape(title_for(status))}</title>"
                f"<h1>{escape(title_for(status))}</h1>{banner(detail)}<p><a href='/'>오늘 화면</a></p>"
            )
            return HTMLResponse(body, status_code=status, headers=headers)
        resp.status_code = status
        if headers:
            resp.headers.update(headers)
        return resp
