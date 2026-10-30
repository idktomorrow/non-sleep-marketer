"""대시보드 "문서 질문" 탭(R3, ADR 0058, 고급 모드).

- GET /ask: 질문 칸, 최근 질문 20개, 최근 측정.
- POST /ask: 질문 하나(CSRF). 찾은 조각만 근거로 Claude가 답하고, 근거가 없으면 Claude를 부르지 않는다.
- POST /ask/eval: 질문 파일 20개로 검색 측정(CSRF). Claude는 부르지 않는다.
답변은 템플릿에서 자동 이스케이프된다(Claude 출력을 HTML로 넣지 않는다).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from nsm.rag.answer import HINT, QuestionRejected, ask_docs, recent_qa
from nsm.rag.evaluate import EvalRejected, recent_evals, run_eval
from nsm.rag.indexer import stored_stats


def install_ask_routes(
    app: FastAPI, state: Any, check_csrf: Callable[..., Any], page: Callable[..., Any]
) -> None:
    def _rag() -> tuple[Any, Any]:
        return state.get_rag() if state.get_rag else (None, None)

    def _view(request: Request, **extra: Any) -> HTMLResponse:
        cfg = state.get_config()
        st = stored_stats(state.repo.db, "docs")
        embedder, answerer = _rag()
        return page(
            request,
            "ask.html",
            hint=HINT,
            max_chars=cfg.settings.rag.qa_max_chars,
            min_score=cfg.settings.rag.min_score,
            stats=st,
            has_embedder=embedder is not None,
            has_answerer=answerer is not None,
            recent=recent_qa(state.repo.db, 20),
            evals=recent_evals(state.repo.db, 3),
            eval_msg=request.query_params.get("eval_msg"),
            eval_error=request.query_params.get("eval_error"),
            **extra,
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request) -> HTMLResponse:
        return _view(request, result=None, error=None, question="")

    @app.post("/ask", response_class=HTMLResponse, dependencies=[Depends(check_csrf)])
    def ask_post(request: Request, question: str = Form("")) -> HTMLResponse:
        embedder, answerer = _rag()
        try:
            result = ask_docs(
                state.get_config(), state.repo, embedder, answerer, question, state.clock.now()
            )
        except QuestionRejected as e:
            resp = _view(request, result=None, error=str(e), question=question[:1000])
            resp.status_code = 400
            return resp
        return _view(request, result=result, error=None, question=result.question)

    @app.post("/ask/eval", dependencies=[Depends(check_csrf)])
    def ask_eval() -> RedirectResponse:
        from urllib.parse import urlencode

        embedder, _ = _rag()
        try:
            r = run_eval(state.get_config(), state.repo, embedder, state.clock.now())
        except EvalRejected as e:
            return RedirectResponse(
                f"/ask?{urlencode({'eval_error': str(e)})}#eval", status_code=303
            )
        msg = "측정했습니다" + (f"({r.error})" if r.error else "")
        return RedirectResponse(f"/ask?{urlencode({'eval_msg': msg})}#eval", status_code=303)
