"""대시보드 "문서 질문" 탭(R3, ADR 0058, 고급 모드).

- GET /ask: 질문 칸, 최근 질문 20개, 최근 측정. `?qa=<기록 id>`면 그 기록(답·근거·비용·시간)을 보여 준다.
- 출처 고르기(R4, ADR 0065): 전체·프로젝트 문서·수집 자료·우리가 올린 글. 고른 값은 검색의 source_kind로 간다.
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
    NO_GROUND_ANSWER,
    SUMMARY_PATH,
    QuestionRejected,
    ask_docs,
    delete_all_qa,
    delete_qa,
    get_qa,
    recent_qa,
)
from nsm.rag.evaluate import EvalRejected, recent_evals, run_eval
from nsm.rag.indexer import stored_stats
from nsm.rag.multi import KIND_BADGE, KIND_NAME, KINDS

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
# 출처 고르기 칩(R6): (값, 칩 글자, 전체 이름). 값은 R4 그대로라 서버 쪽은 바뀌지 않는다
SOURCE_CHOICES = [("", "전체", "전체"), *((k, KIND_BADGE[k], KIND_NAME[k]) for k in KINDS)]


def pick_source(value: str | None) -> str:
    """폼·주소창의 출처 값. 모르는 값은 전체("")로(주소창을 고쳐도 아무 출처나 넘어가지 않게)."""
    return value if value in KINDS else ""


MISSING_QA = "그 질문 기록이 없습니다(지웠거나 보관 기간이 지났습니다)."
EVAL_ERROR_KEY = "doc_eval_last_error"  # 마지막 측정이 멈춘 이유(화면에 함께 보임)


def present(qa: dict[str, Any], krw_per_usd: float, tz: str = "Asia/Seoul") -> dict[str, Any]:
    """기록 하나를 화면용으로: 초·원화, 답 HTML(이스케이프 + 백틱 코드 + [n] 링크)."""
    ns = {int(s["n"]) for s in qa.get("sources", [])}
    sources = []
    for s in qa.get("sources", []):
        # 접힌 줄의 짧은 제목·보조 제목(R6). 전체 경로(heading)는 펼쳤을 때 보인다
        title, subtitle = ask_format.source_title(s.get("heading"), s.get("kind"))
        sources.append(
            {
                **s,
                "badge": KIND_BADGE.get(s.get("kind") or "docs", "문서"),
                "title": title,
                "subtitle": subtitle,
                "link_items": ask_format.post_links(s.get("links")),  # 우리 글 근거의 글 보기(R4d)
                "metric_lines": ask_format.post_metrics(s.get("metrics"), tz),  # 성과 지표(R5)
            }
        )
    return {
        **qa,
        "sources": sources,
        "answer_html": ask_format.answer_html(qa.get("answer"), ns),
        "secs": ask_format.seconds(qa.get("ms")),
        "cost": ask_format.cost(qa.get("usd"), krw_per_usd),
    }


def summary_ids(db: Any, ids: list[str]) -> set[str]:
    """최근 질문 카드의 "요약" 배지(R6): 근거의 path가 성과 요약 조각인 기록 id.

    path 칸만 본다(본문·제목에 같은 글이 있어도 배지가 붙지 않게). 근거 본문은 파이썬으로 읽지 않는다.
    """
    if not ids:
        return set()
    marks = ", ".join("?" * len(ids))
    rows = db.execute(
        f"SELECT id FROM doc_qa WHERE id IN ({marks}) "
        "AND list_contains(json_extract_string(sources, '$[*].path'), ?)",
        [*ids, SUMMARY_PATH],
    )
    return {r[0] for r in rows}


def install_ask_routes(
    app: FastAPI, state: Any, check_csrf: Callable[..., Any], page: Callable[..., Any]
) -> None:
    def _rag() -> tuple[Any, Any]:
        return state.get_rag() if state.get_rag else (None, None)

    def _view(request: Request, **extra: Any) -> HTMLResponse:
        cfg = state.get_config()
        krw = cfg.settings.krw_per_usd
        st = stored_stats(state.repo.db)
        by_kind = [
            {"badge": KIND_BADGE[k], **stored_stats(state.repo.db, k)} for k in KINDS
        ]  # 상단 안내: 출처별 조각 수
        embedder, answerer = _rag()
        q = request.query_params
        last_error = state.repo.get_setting(EVAL_ERROR_KEY, "") if q.get("eval") else ""
        recent = recent_qa(state.repo.db, 20)
        with_summary = summary_ids(state.repo.db, [r["id"] for r in recent])
        return page(
            request,
            "ask.html",
            hint=HINT,
            qa_cost_hint=ask_format.estimate_won(cfg.settings.models.qa, krw),
            max_chars=cfg.settings.rag.qa_max_chars,
            min_score=cfg.settings.rag.min_score,
            stats=st,
            by_kind=by_kind,
            source_choices=SOURCE_CHOICES,
            source=pick_source(extra.pop("source", None) or q.get("source")),
            has_embedder=embedder is not None,
            has_answerer=answerer is not None,
            # 목록은 비용 표시만(답 HTML은 상세에서만 만든다)
            recent=[
                {**r, "cost": ask_format.cost(r["usd"], krw), "summary": r["id"] in with_summary}
                for r in recent
            ],
            no_ground=NO_GROUND_ANSWER,
            evals=recent_evals(state.repo.db, 3),
            eval_notice=EVAL_TEXT.get(q.get("eval", "")),
            # 멈춘 이유(몇 번째 질문·오류 종류)는 주소창이 아니라 서버에 둔 마지막 측정 오류에서
            eval_detail=last_error if q.get("eval") in ("stopped", "error") else "",
            qa_notice=QA_TEXT.get(q.get("done", "")),
            **extra,
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, qa: str = "") -> HTMLResponse:
        result = None
        if qa:
            rec = get_qa(state.repo.db, qa)
            if rec is not None:
                cfg = state.get_config()
                result = present(rec, cfg.settings.krw_per_usd, cfg.settings.timezone)
        return _view(
            request,
            result=result,
            error=MISSING_QA if qa and result is None else None,
            question=result["question"] if result else "",
        )

    @app.post("/ask", response_model=None, dependencies=[Depends(check_csrf)])
    def ask_post(
        request: Request, question: str = Form(""), source: str = Form("")
    ) -> HTMLResponse | RedirectResponse:
        embedder, answerer = _rag()
        kind = pick_source(source)
        try:
            result = ask_docs(
                state.get_config(),
                state.repo,
                embedder,
                answerer,
                question,
                state.clock.now(),
                source_kind=kind or None,
            )
        except QuestionRejected as e:
            resp = _view(request, result=None, error=str(e), question=question[:1000], source=kind)
            resp.status_code = 400
            return resp
        # 저장한 기록으로 보낸다: 새로고침해도 다시 묻지 않는다(R3c). 고른 출처는 다음 질문에도 그대로
        tail = f"&source={kind}" if kind else ""
        return RedirectResponse(f"/ask?qa={result.id}{tail}#answer", status_code=303)

    @app.post("/ask/qa/{qa_id}/delete", dependencies=[Depends(check_csrf)])
    def ask_delete(qa_id: str) -> RedirectResponse:
        delete_qa(state.repo.db, qa_id)
        return RedirectResponse("/ask?done=deleted#recent", status_code=303)

    @app.post("/ask/qa/delete-all", dependencies=[Depends(check_csrf)])
    def ask_delete_all(confirm_all: str = Form("")) -> RedirectResponse:
        if confirm_all != "yes":  # 확인 한 번 더(화면의 확인 창을 거친 폼만 이 값을 보낸다)
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
            state.repo.set_setting(EVAL_ERROR_KEY, str(e)[:300])
            return RedirectResponse(f"/ask?eval={code}#eval", status_code=303)
        return RedirectResponse("/ask?eval=ok#eval", status_code=303)
