"""대시보드 "문서 질문" 탭(R3, ADR 0058, 고급 모드).

- GET /ask: 질문 칸, 최근 질문 20개, 최근 측정. `?qa=<기록 id>`면 그 기록(답·근거·비용·시간)을 보여 준다.
- POST /ask: 질문 하나(CSRF). 처리해 doc_qa에 저장한 뒤 303으로 `/ask?qa=<id>`에 보낸다(R3c): 새로고침(F5)이
  같은 질문을 다시 보내 비용이 또 나가지 않게. 거절(길이·상한·키 없음)은 그 자리에 400으로 보인다.
- POST /ask/qa/<id>/delete, POST /ask/qa/delete-all: 질문 기록 지우기(CSRF, 전부 지우기는 확인 한 번 더).
- POST /ask/eval: 질문 파일 20개로 검색 측정(CSRF). 결과 안내는 주소창에 코드만, 문구는 서버가 고른다.
답 글은 이스케이프한 뒤 백틱 코드와 [n] 링크만 만든다(`ask_format.answer_html`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from nsm.app import ask_format
from nsm.rag.answer import (
    HINT,
    QuestionRejected,
    ask_docs,
    delete_all_qa,
    delete_qa,
    get_qa,
    recent_qa,
)
from nsm.rag.evaluate import EvalRejected, recent_evals, run_eval
from nsm.rag.indexer import stored_stats

# 측정 결과 코드 → 화면 문구. 모르는 코드는 무시한다(주소창을 고쳐도 아무 글이나 뜨지 않게)
EVAL_TEXT = {
    "ok": ("info", "측정했습니다."),
    "limit": (
        "bad",
        "오늘 남은 Gemini 무료 한도가 모자라 질문 20개를 다 잴 수 없습니다. 내일(한국 오후 4~5시 뒤) 다시 눌러 주세요.",
    ),
    "no_key": ("bad", "Gemini 키가 없어 측정할 수 없습니다. 설정 → API 키에서 넣어 주세요."),
    "cost_cap": ("bad", "오늘 비용 상한에 닿아 측정하지 않았습니다."),
    "no_file": ("bad", "측정 질문 파일(tests/fixtures/rag/questions.yaml)이 없습니다."),
    "stopped": (
        "bad",
        "측정 도중 Gemini 요청이 막혀 멈췄습니다. 질문 일부로 만든 표는 비교가 안 돼 남기지 않았습니다.",
    ),
    "error": ("bad", "측정하지 못했습니다."),
}
QA_TEXT = {
    "deleted": "질문 기록을 지웠습니다.",
    "deleted_all": "질문 기록을 모두 지웠습니다.",
}
MISSING_QA = "그 질문 기록이 없습니다(지웠거나 보관 기간이 지났습니다)."


def present(qa: dict[str, Any], krw_per_usd: float) -> dict[str, Any]:
    """기록 하나를 화면용으로: 초·원화, 답 HTML(이스케이프 + 백틱 코드 + [n] 링크)."""
    ns = {int(s["n"]) for s in qa.get("sources", [])}
    return {
        **qa,
        "answer_html": ask_format.answer_html(qa.get("answer"), ns),
        "secs": ask_format.seconds(qa.get("ms")),
        "cost": ask_format.cost(qa.get("usd"), krw_per_usd),
    }


def install_ask_routes(
    app: FastAPI, state: Any, check_csrf: Callable[..., Any], page: Callable[..., Any]
) -> None:
    def _rag() -> tuple[Any, Any]:
        return state.get_rag() if state.get_rag else (None, None)

    def _view(request: Request, **extra: Any) -> HTMLResponse:
        cfg = state.get_config()
        krw = cfg.settings.krw_per_usd
        st = stored_stats(state.repo.db, "docs")
        embedder, answerer = _rag()
        q = request.query_params
        return page(
            request,
            "ask.html",
            hint=HINT,
            max_chars=cfg.settings.rag.qa_max_chars,
            min_score=cfg.settings.rag.min_score,
            stats=st,
            has_embedder=embedder is not None,
            has_answerer=answerer is not None,
            recent=[present(r, krw) for r in recent_qa(state.repo.db, 20)],
            evals=recent_evals(state.repo.db, 3),
            eval_notice=EVAL_TEXT.get(q.get("eval", "")),
            qa_notice=QA_TEXT.get(q.get("done", "")),
            **extra,
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, qa: str = "") -> HTMLResponse:
        result = None
        if qa:
            rec = get_qa(state.repo.db, qa)
            if rec is not None:
                result = present(rec, state.get_config().settings.krw_per_usd)
        return _view(
            request,
            result=result,
            error=MISSING_QA if qa and result is None else None,
            question=result["question"] if result else "",
        )

    @app.post("/ask", response_model=None, dependencies=[Depends(check_csrf)])
    def ask_post(request: Request, question: str = Form("")) -> HTMLResponse | RedirectResponse:
        embedder, answerer = _rag()
        try:
            result = ask_docs(
                state.get_config(), state.repo, embedder, answerer, question, state.clock.now()
            )
        except QuestionRejected as e:
            resp = _view(request, result=None, error=str(e), question=question[:1000])
            resp.status_code = 400
            return resp
        # 저장한 기록으로 보낸다: 새로고침해도 다시 묻지 않는다(R3c)
        return RedirectResponse(f"/ask?qa={result.id}#answer", status_code=303)

    @app.post("/ask/qa/{qa_id}/delete", dependencies=[Depends(check_csrf)])
    def ask_delete(qa_id: str) -> RedirectResponse:
        delete_qa(state.repo.db, qa_id)
        return RedirectResponse("/ask?done=deleted#recent", status_code=303)

    @app.post("/ask/qa/delete-all", dependencies=[Depends(check_csrf)])
    def ask_delete_all(confirm: str = Form("")) -> RedirectResponse:
        if confirm != "yes":  # 확인 한 번 더(화면의 확인 창을 거친 폼만 이 값을 보낸다)
            return RedirectResponse("/ask#recent", status_code=303)
        delete_all_qa(state.repo.db)
        return RedirectResponse("/ask?done=deleted_all#recent", status_code=303)

    @app.post("/ask/eval", dependencies=[Depends(check_csrf)])
    def ask_eval() -> RedirectResponse:
        embedder, _ = _rag()
        try:
            run_eval(state.get_config(), state.repo, embedder, state.clock.now())
        except EvalRejected as e:
            code = e.code if e.code in EVAL_TEXT else "error"
            return RedirectResponse(f"/ask?eval={code}#eval", status_code=303)
        return RedirectResponse("/ask?eval=ok#eval", status_code=303)
