"""R3d: 검색 측정의 유출 조각 제외. 측정 질문이 문서(devlog 등)에 원문으로 실린 조각은 측정에서만 뺀다.
FakeEmbedder·가짜 Claude, 네트워크 없음."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nsm.config.schema import RagSettings
from nsm.rag.answer import ClaudeAnswerer, ask_docs
from nsm.rag.evaluate import leaked_by_question, normalize, recent_evals, run_eval
from nsm.rag.indexer import index_documents
from nsm.rag.sources import DocsFolderSource
from nsm.store.repo import Repository
from tests.app.test_ask_view import FakeMessages
from tests.fakes import FakeEmbedder

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a, **k):
        raise AssertionError("테스트에서 네트워크 호출 금지")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


@pytest.fixture
def env(db, clock, app_config, tmp_path: Path):
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    (root / "devlog").mkdir()
    (root / "adr" / "0008-guard.md").write_text(
        "# 0008. 보호 모드\n\n## 결정\n\n보호 모드는 처음 20건을 사람이 승인해야 게시하는 안전장치다.\n",
        "utf-8",
    )
    (root / "adr" / "0016-naver.md").write_text(
        "# 0016. 네이버 블로그 게시 제외\n\n## 결정\n\n네이버 블로그는 게시하지 않고 수집만 한다.\n",
        "utf-8",
    )
    # 측정 질문이 원문 그대로 실린 devlog(시험 문제가 교과서에 실린 셈)
    (root / "devlog" / "2026-10-30-batch-R2.md").write_text(
        "# R2\n\n## 질문 1\n\n보호 모드가 뭐야?\n\n## 질문 16\n\n오늘 서울 날씨 어때\n", "utf-8"
    )
    repo = Repository(db, clock)
    emb = FakeEmbedder()
    r = RagSettings()
    index_documents(
        db, DocsFolderSource(root, r.include, r.exclude), emb, size=500, overlap=80, now=NOW
    )
    qfile = tmp_path / "q.yaml"
    qfile.write_text(
        "questions:\n"
        "  - {q: 보호 모드가 뭐야, answerable: true, expected: [adr/0008-guard.md], heading: 보호}\n"
        "  - {q: 네이버 블로그는 왜 게시 안 해, answerable: true, expected: [adr/0016-naver.md], heading: 네이버}\n"
        "  - {q: 오늘 서울 날씨 어때, answerable: false}\n",
        "utf-8",
    )
    return {"db": db, "repo": repo, "emb": emb, "cfg": app_config, "qfile": qfile}


def test_normalize_ignores_spaces_punctuation_and_case() -> None:
    assert normalize("보호 모드가 뭐야?") == normalize("보호모드가  뭐야") == "보호모드가뭐야"
    assert normalize("API 키는 어디에 저장돼") == "api키는어디에저장돼"


def test_leaked_chunks_are_found_per_question(env) -> None:
    qs = ["보호 모드가 뭐야", "오늘 서울 날씨 어때", "네이버 블로그는 왜 게시 안 해"]
    m = leaked_by_question(env["db"], qs, env["emb"].model, env["emb"].dim)
    assert len(m["보호 모드가 뭐야"]) == 1 and len(m["오늘 서울 날씨 어때"]) == 1
    assert m["네이버 블로그는 왜 게시 안 해"] == set()
    assert m["보호 모드가 뭐야"] != m["오늘 서울 날씨 어때"]  # 질문마다 자기 문장이 실린 조각만


def test_chunk_quoting_another_question_stays_for_this_question(env) -> None:
    """질문 A가 실린 조각은 질문 B의 측정에선 빼지 않는다(정당한 답일 수 있다)."""
    from nsm.rag.search import search

    m = leaked_by_question(env["db"], ["오늘 서울 날씨 어때"], env["emb"].model, env["emb"].dim)
    res = search(
        env["db"],
        env["emb"],
        "보호 모드가 뭐야",
        k=5,
        min_score=0.0,
        exclude_ids=m["오늘 서울 날씨 어때"],
    )
    assert any(c.path == "devlog/2026-10-30-batch-R2.md" for c in res.candidates)


def test_only_comparable_chunks_are_counted_and_short_questions_skipped(env) -> None:
    db = env["db"]
    db.execute("UPDATE doc_chunks SET embedding = NULL WHERE text LIKE '%오늘 서울 날씨%'")
    m = leaked_by_question(db, ["오늘 서울 날씨 어때", "보호"], env["emb"].model, env["emb"].dim)
    assert m["오늘 서울 날씨 어때"] == set()  # 벡터 없는 조각은 검색이 안 보므로 세지 않음
    assert m["보호"] == set()  # 정규화 뒤 6자 미만은 비교 안 함
    assert (
        leaked_by_question(db, ["보호 모드가 뭐야"], "other-model", env["emb"].dim)[
            "보호 모드가 뭐야"
        ]
        == set()
    )


def test_eval_excludes_leaked_chunks_and_still_counts_the_answer(env) -> None:
    r = run_eval(env["cfg"], env["repo"], env["emb"], NOW, env["qfile"])
    assert r.leaked == 2
    own = {"보호 모드가 뭐야": "질문 1", "오늘 서울 날씨 어때": "질문 16"}  # 각 질문이 실린 절
    for d in r.details:
        if d["q"] in own:  # 자기 문장이 실린 절만 후보에서 빠진다(다른 질문의 절은 남을 수 있음)
            assert all(not c["heading"].endswith(own[d["q"]]) for c in d["cands"])
    guard = next(d for d in r.details if d["q"] == "보호 모드가 뭐야")
    assert guard["cands"][0]["path"] == "adr/0008-guard.md"  # 정답 조각은 그대로 1등
    from nsm.rag.evaluate import table

    row = table(r.details, thresholds=(0.0,))[0]  # 가짜 벡터는 점수가 낮아 기준값 0으로 센다
    assert row.top1 == 2 and row.top3 == 2
    saved = recent_evals(env["db"], 1)[0]
    assert saved.leaked == 2  # doc_eval에 남는다


def test_without_exclusion_the_leaked_chunk_would_win(env) -> None:
    """제외가 왜 필요한지: 같은 검색을 제외 없이 하면 질문이 실린 devlog가 1등이다."""
    from nsm.rag.search import search

    res = search(env["db"], env["emb"], "보호 모드가 뭐야", k=3, min_score=0.0)
    assert res.candidates[0].path == "devlog/2026-10-30-batch-R2.md"


def test_real_questions_do_not_exclude_anything(env) -> None:
    msgs = FakeMessages()
    ans = ClaudeAnswerer(SimpleNamespace(messages=msgs), "claude-haiku-5-5", None)
    env["cfg"].settings.rag.min_score = 0.0
    r = ask_docs(env["cfg"], env["repo"], env["emb"], ans, "보호 모드가 뭐야", NOW)
    assert any(
        s["path"] == "devlog/2026-10-30-batch-R2.md" for s in r.sources
    )  # 실제 질문엔 그대로


def test_eval_screen_shows_leaked_count(env, clock, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from nsm.app.server import AppState, create_app
    from tests.app.test_dashboard import _run_now

    run_eval(env["cfg"], env["repo"], env["emb"], NOW, env["qfile"])
    state = AppState(
        repo=env["repo"],
        clock=clock,
        get_config=lambda: env["cfg"],
        set_config=lambda c: None,
        submit=_run_now,
        adapters={},
        media_dir=tmp_path / "media",
        get_rag=lambda: (env["emb"], None),
    )
    html = TestClient(create_app(state)).get("/ask").text
    assert "유출 조각 2개 제외" in html
    assert html.index("유출 조각 2개 제외") < html.index('class="eval-table"')  # 표 위에


def test_old_eval_rows_without_leaked_column_value_read_as_zero(env) -> None:
    env["db"].execute(
        "INSERT INTO doc_eval (id, ran_at, chunks, without_vector, summary, details) VALUES ('old', ?, 1, 0, '[]', '[]')",
        [NOW.replace(tzinfo=None)],
    )
    env["db"].execute("UPDATE doc_eval SET leaked = NULL WHERE id = 'old'")
    assert recent_evals(env["db"], 1)[0].leaked == 0
