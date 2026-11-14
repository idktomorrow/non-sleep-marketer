"""주간 리포트 화면: reports 폴더의 마크다운 파일 목록과 본문(본문은 `md_lite`로 읽는 문서로, C45)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from nsm.app.md_lite import report_html

_NAME = re.compile(r"^weekly-\d{4}-\d{2}-\d{2}\.md$")


def make_router(reports_dir: Path, page: Callable[..., HTMLResponse]) -> APIRouter:
    router = APIRouter()

    def files() -> list[str]:
        if not reports_dir.is_dir():
            return []
        return sorted(
            (p.name for p in reports_dir.glob("weekly-*.md") if _NAME.match(p.name)), reverse=True
        )

    @router.get("/reports", response_class=HTMLResponse)
    def report_list(request: Request, name: str | None = None) -> HTMLResponse:
        names = files()
        chosen = name or (names[0] if names else None)
        body = None
        if chosen:
            if not _NAME.match(chosen) or not (reports_dir / chosen).is_file():
                raise HTTPException(404, "리포트가 없습니다")
            body = report_html((reports_dir / chosen).read_text(encoding="utf-8"))
        return page(request, "reports.html", names=names, chosen=chosen, body=body)

    return router
