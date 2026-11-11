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


# ------------------------------------------------------------------ 문서 단위 묶기(R4d, ADR 0067)


class _AxisEmbedder:
    """질문 벡터가 늘 [1, 0]인 임베더. 조각 벡터 [s, √(1-s²)]의 점수가 정확히 s라 순위를 손으로 정한다."""

    model = "axis"
    dim = 2

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]


def _put(db: Database, kind: str, path: str, seq: int, score: float) -> None:
    import math

    db.execute(
        "INSERT INTO doc_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'axis', ?::FLOAT[], ?)",
        [
            f"{kind}:{path}#{seq}",
            kind,
            path,
            f"{path} > {seq}",
            seq,
            f"{path} {seq}",
            1,
            f"h{path}{seq}",
            [score, math.sqrt(1 - score * score)],
            NOW.replace(tzinfo=None),
        ],
    )


@pytest.fixture
def mixed() -> Database:
    """점수 순: 같은 원고(threads·cards·instagram) 셋, 같은 수집 기사 둘, 문서 파일 하나의 두 절, 다른 원고 하나."""
    db = Database()
    for kind, path, seq, s in [
        ("posts", "posts/C1/threads", 0, 0.95),
        ("posts", "posts/C1/cards", 0, 0.94),
        ("posts", "posts/C1/instagram", 0, 0.93),
        ("collected", "source_items/S1", 0, 0.92),
        ("collected", "source_items/S1", 1, 0.91),
        ("docs", "adr/0008-guard.md", 0, 0.90),
        ("docs", "adr/0008-guard.md", 1, 0.89),
        ("posts", "posts/C2/threads", 0, 0.88),
        ("collected", "source_items/S2", 0, 0.70),
    ]:
        _put(db, kind, path, seq, s)
    return db


def _paths(res) -> list[tuple[str, float]]:
    return [(h.path, round(h.score, 2)) for h in res.candidates]


def test_group_key_per_source() -> None:
    from nsm.rag.search import group_key

    assert group_key("posts/C1/threads", "posts") == "posts/C1"
    assert group_key("posts/C1/instagram", "posts") == "posts/C1"
    assert group_key("posts/C1/cards", "posts") == "posts/C1"  # 카드 문서도 같은 원고
    assert group_key("source_items/S1", "collected") == "source_items/S1"
    assert group_key("adr/0008-guard.md", "docs") == "adr/0008-guard.md"
    assert group_key("posts/odd", "posts") == "posts/odd"  # 모양이 다르면 경로 그대로


def test_per_doc_one_fills_k_with_distinct_documents(mixed: Database) -> None:
    res = search(mixed, _AxisEmbedder(), "q", k=5, min_score=0.0, per_doc=1)
    assert _paths(res) == [
        ("posts/C1/threads", 0.95),
        ("source_items/S1", 0.92),
        ("adr/0008-guard.md", 0.9),
        ("posts/C2/threads", 0.88),
        ("source_items/S2", 0.7),
    ]


def test_per_doc_two_keeps_two_per_group_in_score_order(mixed: Database) -> None:
    res = search(mixed, _AxisEmbedder(), "q", k=5, min_score=0.0, per_doc=2)
    assert _paths(res) == [
        ("posts/C1/threads", 0.95),
        ("posts/C1/cards", 0.94),  # cards와 threads가 한 원고: 세 번째(instagram)는 빠진다
        ("source_items/S1", 0.92),
        ("source_items/S1", 0.91),
        ("adr/0008-guard.md", 0.9),
    ]
    scores = [h.score for h in res.candidates]
    assert scores == sorted(scores, reverse=True)


def test_without_per_doc_is_plain_top_k(mixed: Database) -> None:
    res = search(mixed, _AxisEmbedder(), "q", k=3, min_score=0.0)
    assert [p for p, _ in _paths(res)] == [
        "posts/C1/threads",
        "posts/C1/cards",
        "posts/C1/instagram",
    ]


def test_too_few_candidates_returns_what_there_is(mixed: Database) -> None:
    res = search(mixed, _AxisEmbedder(), "q", k=10, min_score=0.0, per_doc=1)
    assert len(res.candidates) == 5  # 묶음이 다섯뿐
    one = search(mixed, _AxisEmbedder(), "q", k=5, min_score=0.0, source_kind="docs", per_doc=1)
    assert [p for p, _ in _paths(one)] == ["adr/0008-guard.md"]


def test_threshold_applies_after_grouping_and_top_score_before(mixed: Database) -> None:
    res = search(mixed, _AxisEmbedder(), "q", k=5, min_score=0.85, per_doc=1)
    assert [h.path for h in res.hits] == [
        "posts/C1/threads",
        "source_items/S1",
        "adr/0008-guard.md",
        "posts/C2/threads",
    ]  # 0.70인 S2는 묶은 뒤 다섯째 자리에 들지만 기준값에서 빠진다
    assert len(res.candidates) == 5 and res.top_score == pytest.approx(0.95)
    none = search(mixed, _AxisEmbedder(), "q", k=5, min_score=0.99, per_doc=1)
    assert not none.grounded and none.top_score == pytest.approx(0.95)


def test_exclude_ids_still_works_with_grouping(mixed: Database) -> None:
    res = search(
        mixed,
        _AxisEmbedder(),
        "q",
        k=2,
        min_score=0.0,
        per_doc=1,
        exclude_ids={"posts:posts/C1/threads#0"},
    )
    assert [p for p, _ in _paths(res)] == ["posts/C1/cards", "source_items/S1"]


def test_pool_is_larger_than_k(monkeypatch) -> None:
    """같은 묶음 조각이 k개를 넘게 앞에 있어도, 후보를 넉넉히(k의 4배, 최소 20개) 가져와 다른 묶음으로 채운다."""
    db = Database()
    for i in range(19):
        _put(db, "docs", "devlog/long.md", i, 0.99 - i * 0.001)
    _put(db, "docs", "adr/other.md", 0, 0.5)
    res = search(db, _AxisEmbedder(), "q", k=2, min_score=0.0, per_doc=1)
    assert [h.path for h in res.candidates] == ["devlog/long.md", "adr/other.md"]


def test_per_doc_setting_default_and_range() -> None:
    from pydantic import ValidationError

    assert RagSettings().per_doc == 1  # R4d 측정으로 고름(ADR 0067)
    assert RagSettings(per_doc=5).per_doc == 5
    for bad in (0, 6):
        with pytest.raises(ValidationError):
            RagSettings(per_doc=bad)


def test_pool_is_four_times_k_when_larger_than_minimum() -> None:
    """k=6이면 후보 24개: 같은 묶음 23조각 뒤 24번째의 다른 문서가 채워진다(25번째는 후보 밖)."""
    db = Database()
    for i in range(23):
        _put(db, "docs", "devlog/long.md", i, 0.99 - i * 0.001)
    _put(db, "docs", "adr/in-pool.md", 0, 0.6)
    _put(db, "docs", "adr/out-of-pool.md", 0, 0.5)
    res = search(db, _AxisEmbedder(), "q", k=6, min_score=0.0, per_doc=1)
    assert [h.path for h in res.candidates] == ["devlog/long.md", "adr/in-pool.md"]


def test_post_links_ignores_odd_shapes() -> None:
    from nsm.app.ask_format import post_links

    assert post_links(None) == [] and post_links(["https://x.test"]) == []  # type: ignore[arg-type]
    assert post_links({"instagram": "https://i.test/p", "threads": "https://t.test/p"}) == [
        ("스레드 글 보기", "https://t.test/p"),
        ("인스타그램 글 보기", "https://i.test/p"),
    ]
