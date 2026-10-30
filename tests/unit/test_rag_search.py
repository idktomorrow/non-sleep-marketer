"""R2: 문서 검색(순위·k·기준값·벡터 없는 조각·비용)과 질문 파일. 네트워크 없음(FakeEmbedder·가짜 HTTP)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from nsm.config.schema import RagSettings
from nsm.rag.gemini import GeminiEmbedder
from nsm.rag.indexer import index_documents
from nsm.rag.search import NO_GROUND, search
from nsm.rag.sources import DocsFolderSource, default_docs_root
from nsm.store.db import Database
from tests.fakes import FakeCostSink, FakeEmbedder

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
QUESTIONS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "questions.yaml"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a, **k):
        raise AssertionError("테스트에서 네트워크 호출 금지")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    (root / "adr" / "0008-guard.md").write_text(
        "# 0008. 보호 모드\n\n## 결정\n\n보호 모드는 처음 20건을 사람이 승인해야 게시하는 안전장치다.\n",
        "utf-8",
    )
    (root / "adr" / "0016-naver.md").write_text(
        "# 0016. 네이버 블로그 게시 제외\n\n## 결정\n\n네이버 블로그는 공식 API가 없어 게시하지 않고 수집만 한다.\n",
        "utf-8",
    )
    (root / "user-guide.md").write_text(
        "# 설명서\n\n## 요청 제한\n\n인스타그램이 HTTP 429를 주면 2시간 쉬었다 다시 한다.\n",
        "utf-8",
    )
    return root


def _indexed(docs: Path, emb: FakeEmbedder) -> Database:
    db = Database()
    r = RagSettings()
    index_documents(
        db, DocsFolderSource(docs, r.include, r.exclude), emb, size=500, overlap=80, now=NOW
    )
    return db


def test_ranks_by_similarity_and_limits_k(docs: Path) -> None:
    emb = FakeEmbedder()
    db = _indexed(docs, emb)
    res = search(db, emb, "보호 모드가 뭐야", k=2, min_score=0.0)
    assert len(res.hits) == 2 and res.hits[0].path == "adr/0008-guard.md"
    assert res.hits[0].score >= res.hits[1].score
    assert res.hits[0].heading_path == "adr/0008-guard.md > 0008. 보호 모드 > 결정"
    assert "20건" in res.hits[0].text and res.grounded and emb.queries == ["보호 모드가 뭐야"]
    one = search(db, emb, "네이버 블로그는 왜 게시 안 해", k=1, min_score=0.0)
    assert [h.path for h in one.hits] == ["adr/0016-naver.md"]


def test_below_threshold_returns_no_ground(docs: Path) -> None:
    emb = FakeEmbedder()
    db = _indexed(docs, emb)
    res = search(db, emb, "오늘 서울 날씨 어때", k=5, min_score=0.99)
    assert res.hits == [] and not res.grounded and res.reason == NO_GROUND
    assert res.top_score is not None and res.top_score < 0.99  # 1등 점수는 측정용으로 남는다
    assert len(res.candidates) == 3


def test_chunks_without_vectors_or_other_model_are_skipped(docs: Path) -> None:
    db = Database()
    r = RagSettings()
    src = DocsFolderSource(docs, r.include, r.exclude)
    index_documents(db, src, None, size=500, overlap=80, now=NOW)  # 키 없이: 벡터 없음
    emb = FakeEmbedder()
    assert search(db, emb, "보호 모드", k=5, min_score=0.0).hits == []
    index_documents(db, src, FakeEmbedder(model="other"), size=500, overlap=80, now=NOW)
    assert (
        search(db, emb, "보호 모드", k=5, min_score=0.0).hits == []
    )  # 다른 모델 벡터와는 비교 안 함
    db.execute("UPDATE doc_chunks SET embedding = NULL WHERE path = 'adr/0008-guard.md'")
    index_documents(db, src, emb, size=500, overlap=80, now=NOW)
    db.execute("UPDATE doc_chunks SET embedding = NULL WHERE path = 'adr/0008-guard.md'")
    paths = {h.path for h in search(db, emb, "보호 모드", k=5, min_score=0.0).hits}
    assert "adr/0008-guard.md" not in paths and paths


def test_empty_question_returns_nothing_without_calling_embedder(docs: Path) -> None:
    emb = FakeEmbedder()
    db = _indexed(docs, emb)
    assert not search(db, emb, "   ", k=5, min_score=0.0).grounded and emb.queries == []


def test_query_embedding_cost_is_recorded_as_embedding_query() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={"embeddings": [{"values": [0.5] * 4}], "usageMetadata": {"promptTokenCount": 12}},
        )

    sink = FakeCostSink()
    e = GeminiEmbedder(
        "k" * 30,
        "gemini-embedding-2",
        4,
        usd_per_mtok=0.2,
        cost_sink=sink,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    assert e.embed_query("보호 모드가 뭐야") == [0.5] * 4
    body = json.loads(seen[0].content)
    assert (
        body["requests"][0]["content"]["parts"][0]["text"]
        == "task: search result | query: 보호 모드가 뭐야"
    )
    kind, provider, usd, ref = sink.records[0]
    assert (kind, provider, ref) == ("classifier", "gemini", "embedding-query")
    assert usd == pytest.approx(12 / 1_000_000 * 0.2)


# ------------------------------------------------------------------ 질문 파일(측정은 devlog, 여기선 형식·결정성)


def _questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]


def test_question_file_has_15_answerable_and_5_unanswerable_with_real_paths() -> None:
    qs = _questions()
    assert len(qs) == 20
    yes = [q for q in qs if q["answerable"]]
    assert len(yes) == 15 and len(qs) - len(yes) == 5
    root = default_docs_root()
    for q in yes:
        assert q["expected"] and q["heading"]
        for path in q["expected"]:
            assert (root / path).is_file(), path  # 문서 이름이 바뀌면 질문 파일도 고친다


def test_question_file_runs_deterministically_with_fake_embedder() -> None:
    """실제 정답 파일만 색인해 20개를 두 번 돌리면 결과가 같다(측정 스크립트와 같은 길, 네트워크 없음)."""
    root = default_docs_root()
    qs = _questions()
    wanted = sorted({p for q in qs if q["answerable"] for p in q["expected"]})

    class Only:
        kind = "docs"

        def documents(self):
            src = DocsFolderSource(root, wanted, [])
            return src.documents()

    db = Database()
    emb = FakeEmbedder()
    index_documents(db, Only(), emb, size=500, overlap=80, now=NOW)

    def run() -> list[tuple[str, list[str]]]:
        return [
            (q["q"], [h.path for h in search(db, emb, q["q"], k=3, min_score=0.0).hits]) for q in qs
        ]

    first = run()
    assert first == run()
    assert all(len(paths) == 3 for _, paths in first)
