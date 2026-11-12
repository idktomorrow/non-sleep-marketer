"""R3c: 문서 질문 손질 — 새로고침 중복 전송 막기, 기록 보기·지우기, 보관 정리, 초·원화, 백틱 코드, [n] 링크, 안내 코드.
Claude·Gemini 모두 가짜, 네트워크 없음(준비물은 R3 테스트와 같다)."""

from __future__ import annotations

from datetime import timedelta

from nsm.app import ask_format
from tests.app.test_ask_view import _ask, _no_network, docs, env  # noqa: F401  같은 준비물

_ = (docs, env, _no_network)


def _ask_follow(env, q: str):
    r = env["client"].post(
        "/ask", data={"question": q, "csrf": env["state"].csrf_token}, follow_redirects=False
    )
    return r


def test_post_redirects_to_saved_record_and_get_shows_it(env) -> None:
    r = _ask_follow(env, "보호 모드가 뭐야")
    assert r.status_code == 303
    loc = r.headers["location"]
    qid = env["repo"].db.execute("SELECT id FROM doc_qa")[0][0]
    assert loc == f"/ask?qa={qid}#answer"
    html = env["client"].get(loc).text
    assert 'id="answer"' in html and "보호 모드는 처음 20건을" in html
    assert "[1]</b>" in html and 'src-path">adr/0008-guard.md' in html  # 근거(번호·전체 경로)
    assert "원문 없음" not in html  # 근거 본문도 다시 보임
    assert "안전장치" in html  # 조각 본문


def test_refresh_does_not_ask_again(env) -> None:
    loc = _ask_follow(env, "보호 모드가 뭐야").headers["location"]
    calls = len(env["msgs"].calls)
    costs = env["repo"].db.execute("SELECT count(*) FROM costs")[0][0]
    for _ in range(3):  # 새로고침 = 같은 GET
        assert env["client"].get(loc).status_code == 200
    assert len(env["msgs"].calls) == calls
    assert env["repo"].db.execute("SELECT count(*) FROM costs")[0][0] == costs
    assert env["repo"].db.execute("SELECT count(*) FROM doc_qa")[0][0] == 1


def test_rejection_still_shows_in_place_with_400(env) -> None:
    r = _ask_follow(env, "가" * 201)
    assert r.status_code == 400 and "200자까지" in r.text


def test_record_detail_link_and_missing_record(env) -> None:
    _ask(env, "보호 모드가 뭐야")
    qid = env["repo"].db.execute("SELECT id FROM doc_qa")[0][0]
    html = env["client"].get("/ask").text
    assert f'href="/ask?qa={qid}#answer"' in html  # 최근 질문 행 → 상세
    assert "기록이 없습니다" in env["client"].get("/ask?qa=nope").text


def test_old_record_without_chunk_text_shows_no_original(env) -> None:
    _ask(env, "보호 모드가 뭐야")
    db = env["repo"].db
    db.execute(
        """UPDATE doc_qa SET sources = '[{"n": 1, "path": "a.md", "heading": "a.md > 가", "score": 0.7}]'"""
    )
    qid = db.execute("SELECT id FROM doc_qa")[0][0]
    assert "원문 없음" in env["client"].get(f"/ask?qa={qid}").text


def test_delete_one_and_delete_all_with_confirm(env) -> None:
    _ask(env, "보호 모드가 뭐야")
    _ask(env, "네이버 블로그는 왜 게시 안 해")
    db = env["repo"].db
    c = env["client"]
    csrf = env["state"].csrf_token
    qid = db.execute("SELECT id FROM doc_qa ORDER BY asked_at, rowid LIMIT 1")[0][0]
    r = c.post(f"/ask/qa/{qid}/delete", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and db.execute("SELECT count(*) FROM doc_qa")[0][0] == 1
    assert "질문 기록을 지웠습니다" in c.get(r.headers["location"]).text
    c.post("/ask/qa/delete-all", data={"csrf": csrf})  # 확인 없이: 지우지 않음
    assert db.execute("SELECT count(*) FROM doc_qa")[0][0] == 1
    r = c.post(
        "/ask/qa/delete-all", data={"csrf": csrf, "confirm_all": "yes"}, follow_redirects=False
    )
    assert db.execute("SELECT count(*) FROM doc_qa")[0][0] == 0
    assert "모두 지웠습니다" in c.get(r.headers["location"]).text


def test_delete_requests_without_csrf_are_rejected(env) -> None:
    _ask(env, "보호 모드가 뭐야")
    db = env["repo"].db
    qid = db.execute("SELECT id FROM doc_qa")[0][0]
    c = env["client"]
    assert c.post(f"/ask/qa/{qid}/delete").status_code == 403
    assert c.post("/ask/qa/delete-all", data={"confirm_all": "yes"}).status_code == 403
    assert db.execute("SELECT count(*) FROM doc_qa")[0][0] == 1


def test_maintenance_prunes_old_questions_but_keeps_evals(env) -> None:
    from nsm.rag.answer import prune_qa
    from nsm.rag.evaluate import EvalResult, save_eval

    _ask(env, "보호 모드가 뭐야")
    db = env["repo"].db
    now = env["state"].clock.now()
    save_eval(db, EvalResult(ran_at=now - timedelta(days=400), chunks=1, without_vector=0))
    assert prune_qa(db, now + timedelta(days=89), 90) == 0
    assert prune_qa(db, now + timedelta(days=91), 90) == 1
    assert db.execute("SELECT count(*) FROM doc_qa")[0][0] == 0
    assert db.execute("SELECT count(*) FROM doc_eval")[0][0] == 1  # 측정 기록은 남김


def test_maintenance_job_reports_qa_pruned(env, app_config, clock) -> None:
    from nsm.engine.context import JobContext
    from nsm.engine.runner import run_job
    from tests.fakes import FakeNotifier

    ctx = JobContext(
        config=app_config, repo=env["repo"], clock=clock, notifier=FakeNotifier(), adapters={}
    )
    res = run_job("maintenance", ctx)
    assert "qa_pruned" in res.counts


def test_seconds_and_won_formatting(env) -> None:
    assert ask_format.seconds(1983) == "2.0초" and ask_format.seconds(63000) == "63.0초"
    assert ask_format.cost(0.000353, 1400) == "$0.0004 (약 0.5원)"
    assert ask_format.cost(0.02, 1400) == "$0.0200 (약 28원)"
    assert ask_format.cost(0, 1400) == "$0 (0원)"
    env["cfg"].settings.krw_per_usd = 1000
    _ask(env, "보호 모드가 뭐야")
    html = env["client"].get("/ask").text
    assert "(약 0.3원)" in html  # 가짜 사용량 2,000·200토큰 → $0.0003 × 1,000원


def test_backticks_become_code_and_html_stays_escaped() -> None:
    out = str(
        ask_format.answer_html(
            "`settings.protect_count: 10`로 둡니다 <script>alert(1)</script> [1]", {1}
        )
    )
    assert "<code>settings.protect_count: 10</code>" in out
    assert "<script>" not in out and "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert (
        str(ask_format.answer_html("`<b>굵게</b>`", set()))
        == "<code>&lt;b&gt;굵게&lt;/b&gt;</code>"
    )


def test_citations_link_only_to_existing_sources() -> None:
    out = str(ask_format.answer_html("답입니다 [1][3]. 다른 것 [9].", {1, 3}))
    assert '<a class="cite" href="#src-1" data-src="1">[1]</a>' in out
    assert '<a class="cite" href="#src-3" data-src="3">[3]</a>' in out
    assert "[9]" in out and 'href="#src-9"' not in out


def test_answer_page_renders_code_links_and_escapes(env) -> None:
    env["msgs"].text = "`settings.protect_count: 10` 입니다 [1] <script>x()</script>"
    loc = _ask_follow(env, "보호 모드가 뭐야").headers["location"]
    html = env["client"].get(loc).text
    assert "<code>settings.protect_count: 10</code>" in html
    assert 'href="#src-1"' in html and 'id="src-1"' in html
    assert "<script>x()</script>" not in html


def test_eval_notice_uses_codes_and_ignores_unknown(env) -> None:
    c = env["client"]
    assert "측정했습니다." in c.get("/ask?eval=ok").text
    assert "Gemini 키가 없어 측정할 수 없습니다" in c.get("/ask?eval=no_key").text
    page = c.get("/ask?eval=<script>alert(1)</script>").text
    assert "alert(1)" not in page and "측정했습니다." not in page


def test_busy_script_is_bundled_and_loaded(env) -> None:
    html = env["client"].get("/ask").text
    assert "/static/ask.js" in html and "data-busy-text" in html
    js = env["client"].get("/static/ask.js").text
    assert "disabled = true" in js and "a.cite" in js


# ------------------------------------------------------------------ R3c 리뷰 반영


def test_deleting_history_does_not_reset_the_daily_cap(env) -> None:
    env["cfg"].settings.rag.qa_daily_limit = 2
    _ask(env, "보호 모드")
    _ask(env, "네이버 블로그")
    c = env["client"]
    c.post("/ask/qa/delete-all", data={"csrf": env["state"].csrf_token, "confirm_all": "yes"})
    r = _ask(env, "또 묻기")
    assert r.status_code == 400 and "질문 수 상한(2개)" in r.text


def test_brackets_inside_code_are_not_links() -> None:
    out = str(ask_format.answer_html("`arr[1]`을 봅니다 [1]", {1}))
    assert "<code>arr[1]</code>" in out and out.count('class="cite"') == 1


def test_eval_stop_reason_is_shown_from_server_not_url(env, tmp_path, monkeypatch) -> None:
    from nsm.rag.gemini import EmbeddingError
    from tests.fakes import FakeEmbedder

    class Flaky(FakeEmbedder):
        def embed_query(self, question: str) -> list[float]:
            if len(self.queries) >= 1:
                raise EmbeddingError("429", "rate_limited")
            return super().embed_query(question)

    env["holder"]["emb"] = Flaky()
    r = env["client"].post(
        "/ask/eval", data={"csrf": env["state"].csrf_token}, follow_redirects=False
    )
    assert r.headers["location"] == "/ask?eval=stopped#eval"
    html = env["client"].get(r.headers["location"]).text
    assert "2번째 질문에서 멈춰" in html and "rate_limited" in html
    assert "2번째" not in env["client"].get("/ask").text  # 안내 코드가 없으면 보이지 않음


def test_plan_carries_a_code() -> None:
    from types import SimpleNamespace

    from nsm.rag.budget import plan_embedding

    cfg = SimpleNamespace(
        settings=SimpleNamespace(
            timezone="Asia/Seoul",
            daily_cost_cap_usd=3.0,
            rag=SimpleNamespace(embed_daily_limit=1000),
        )
    )
    assert plan_embedding(cfg, None, None, None, 1).code == "no_key"


def test_recent_list_reads_counts_not_chunk_text(env) -> None:
    from nsm.rag.answer import recent_qa

    _ask(env, "보호 모드가 뭐야")
    row = recent_qa(env["repo"].db, 20)[0]
    assert row["source_count"] >= 1 and "sources" not in row


def test_cost_hint_follows_model_and_rate(env) -> None:
    env["cfg"].settings.krw_per_usd = 1000
    html = env["client"].get("/ask").text
    assert "Claude 약 0.3원" in html  # haiku 입력 2,000·출력 250토큰 어림 × 1,000원


def test_delete_all_form_uses_window_confirm_and_resets_busy_on_back(env) -> None:
    html = env["client"].get("/ask").text
    _ask(env, "보호 모드가 뭐야")
    html = env["client"].get("/ask").text
    assert (
        "window.confirm(" in html and 'name="confirm_all"' in html and 'name="confirm"' not in html
    )
    js = env["client"].get("/static/ask.js").text
    assert "pageshow" in js and "persisted" in js
