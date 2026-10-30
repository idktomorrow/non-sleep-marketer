"""R1: 색인(증분·삭제·제외·키 없음·저장 왕복)과 Gemini 임베더(가짜 HTTP). 네트워크 호출 없음."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nsm.config.schema import RagSettings
from nsm.rag.gemini import EmbeddingError, GeminiEmbedder, as_document
from nsm.rag.indexer import index_documents, stored_stats
from nsm.rag.sources import DocsFolderSource
from nsm.store.db import Database
from tests.fakes import FakeCostSink, FakeEmbedder

NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 테스트는 실제 네트워크를 부르면 실패한다."""

    def deny(*a, **k):
        raise AssertionError("테스트에서 네트워크 호출 금지")

    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", deny
    )  # 가짜 전송(MockTransport)은 그대로


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "adr").mkdir(parents=True)
    (root / "presentation").mkdir()
    (root / "adr" / "0001-first.md").write_text(
        "# 0001. 첫 결정\n\n## 결정\n\n이렇게 한다.\n", "utf-8"
    )
    (root / "adr" / "0000-template.md").write_text("# 0000. 양식\n\n## 결정\n\n빈칸\n", "utf-8")
    (root / "user-guide.md").write_text("# 설명서\n\n## 설치\n\nuv sync를 합니다.\n", "utf-8")
    (root / "presentation" / "talk.md").write_text("# 발표\n\n비공개 자료\n", "utf-8")
    (root / "notes.md").write_text("# 포함 목록에 없는 문서\n\n본문\n", "utf-8")
    return root


def _source(root: Path) -> DocsFolderSource:
    r = RagSettings()
    return DocsFolderSource(root, r.include, r.exclude)


def _index(db, root, emb, **kw):
    return index_documents(db, _source(root), emb, size=500, overlap=80, now=NOW, **kw)


def test_excluded_and_unlisted_files_are_not_indexed(docs: Path) -> None:
    paths = [d.path for d in _source(docs).documents()]
    assert paths == ["adr/0001-first.md", "user-guide.md"]  # 양식·발표·목록 밖 문서 제외


def test_store_and_read_back_vectors(docs: Path) -> None:
    db = Database()
    emb = FakeEmbedder(dim=8)
    rep = _index(db, docs, emb)
    assert rep.files == 2 and rep.chunks == 2 and rep.embedded == 2 and rep.without_vector == 0
    rows = db.execute(
        "SELECT path, heading_path, seq, text, chars, embed_model, embedding FROM doc_chunks ORDER BY path"
    )
    path, heading, seq, text, chars, model, vec = rows[0]
    assert (path, heading, seq) == (
        "adr/0001-first.md",
        "adr/0001-first.md > 0001. 첫 결정 > 결정",
        0,
    )
    assert text.startswith(heading + "\n") and chars == len(text) and model == "fake-embed"
    assert vec == pytest.approx(emb.embed([text])[0], abs=1e-6)  # FLOAT[]로 저장·읽기 왕복
    st = stored_stats(db)
    assert st["chunks"] == 2 and st["files"] == 2 and st["without_vector"] == 0


def test_unchanged_content_is_not_embedded_again(docs: Path) -> None:
    db = Database()
    _index(db, docs, FakeEmbedder())
    again = FakeEmbedder()
    rep = _index(db, docs, again)
    assert again.calls == [] and rep.reused == 2 and rep.embedded == 0
    (docs / "user-guide.md").write_text(
        "# 설명서\n\n## 설치\n\nuv sync를 합니다. 바뀐 줄.\n", "utf-8"
    )
    third = FakeEmbedder()
    rep = _index(db, docs, third)
    assert len(third.calls) == 1 and len(third.calls[0]) == 1  # 바뀐 조각 하나만
    assert rep.reused == 1 and rep.embedded == 1


def test_model_change_re_embeds(docs: Path) -> None:
    db = Database()
    _index(db, docs, FakeEmbedder(model="m1"))
    other = FakeEmbedder(model="m2")
    _index(db, docs, other)
    assert sum(len(c) for c in other.calls) == 2


def test_no_key_saves_chunks_then_embeds_only_missing_later(docs: Path) -> None:
    db = Database()
    rep = _index(db, docs, None)
    assert rep.no_key and rep.chunks == 2 and rep.without_vector == 2
    assert db.execute("SELECT count(*) FROM doc_chunks WHERE embedding IS NULL")[0][0] == 2
    emb = FakeEmbedder()
    rep = _index(db, docs, emb)  # 키가 생김: 벡터 없던 조각만
    assert sum(len(c) for c in emb.calls) == 2 and rep.without_vector == 0
    assert _index(db, docs, FakeEmbedder()).embedded == 0


def test_deleted_file_removes_its_chunks(docs: Path) -> None:
    db = Database()
    _index(db, docs, FakeEmbedder())
    (docs / "user-guide.md").unlink()
    rep = _index(db, docs, FakeEmbedder())
    assert rep.deleted_files == ["user-guide.md"]
    assert [r[0] for r in db.execute("SELECT DISTINCT path FROM doc_chunks")] == [
        "adr/0001-first.md"
    ]


def test_rate_limit_keeps_progress_and_next_run_resumes(docs: Path) -> None:
    db = Database()
    emb = FakeEmbedder(fail_after=0)
    emb.batch_size = 1
    rep = _index(db, docs, emb)
    assert rep.error_kind == "rate_limited" and rep.without_vector == 2 and rep.chunks == 2
    ok = FakeEmbedder()
    assert _index(db, docs, ok).without_vector == 0


def test_rebuild_embeds_everything_again(docs: Path) -> None:
    db = Database()
    _index(db, docs, FakeEmbedder())
    emb = FakeEmbedder()
    _index(db, docs, emb, rebuild=True)
    assert sum(len(c) for c in emb.calls) == 2


def test_migration_from_v5_adds_doc_chunks(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "old.duckdb"
    Database(path)._conn.close()
    c = duckdb.connect(str(path))
    c.execute("DROP TABLE doc_chunks")
    c.execute("DELETE FROM schema_version")
    c.execute("INSERT INTO schema_version VALUES (5)")
    c.close()
    db = Database(path)
    assert db.schema_version == 6
    assert db.execute("SELECT count(*) FROM doc_chunks")[0][0] == 0


# ------------------------------------------------------------------ Gemini 임베더(가짜 HTTP)


def _client(responses: list[httpx.Response], seen: list[httpx.Request]) -> httpx.Client:
    it = iter(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return next(it)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(n: int, dim: int, tokens: int = 50) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "embeddings": [{"values": [0.1] * dim} for _ in range(n)],
            "usageMetadata": {"promptTokenCount": tokens},
        },
    )


def test_gemini_batches_with_dimension_document_format_and_interval() -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    sink = FakeCostSink()
    e = GeminiEmbedder(
        "fake-gemini-key-0000000000",
        "gemini-embedding-2",
        4,
        batch_size=2,
        interval_s=1.5,
        usd_per_mtok=0.20,
        cost_sink=sink,
        client=_client([_ok(2, 4), _ok(1, 4)], seen),
        sleep=sleeps.append,
    )
    out = e.embed(["a.md > 제목\n본문1", "a.md > 제목\n본문2", "b.md\n본문3"])
    assert len(out) == 3 and all(len(v) == 4 for v in out)
    assert len(seen) == 2 and sum(sleeps) == 1.5  # 묶음 2번, 사이 간격 1번
    req = seen[0]
    assert req.url.path.endswith("/models/gemini-embedding-2:batchEmbedContents")
    assert req.headers["x-goog-api-key"] == "fake-gemini-key-0000000000"
    assert "fake-gemini-key" not in str(req.url)
    body = json.loads(req.content)
    first = body["requests"][0]
    assert first["model"] == "models/gemini-embedding-2" and first["outputDimensionality"] == 4
    assert first["content"]["parts"][0]["text"] == "title: a.md > 제목 | text: 본문1"
    assert e.tokens == 100
    kind, provider, usd, ref = sink.records[0]
    assert (kind, provider, ref) == ("classifier", "gemini", "embedding")
    assert usd == pytest.approx(50 / 1_000_000 * 0.20)


def test_gemini_minute_429_waits_once_then_raises_rate_limited() -> None:
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    e = GeminiEmbedder(
        "k" * 30,
        "gemini-embedding-2",
        4,
        client=_client([httpx.Response(429)] * 2, seen),
        sleep=sleeps.append,
    )
    with pytest.raises(EmbeddingError) as exc:
        e.embed(["x\ny"])
    assert exc.value.kind == "rate_limited" and exc.value.retry_at is not None
    assert len(seen) == 2 and sum(sleeps) == 61

    from nsm.domain.errors import kind_of

    assert kind_of(exc.value) == "rate_limited"  # 오류 종류 표(ADR 0054)로 흘러간다


def test_gemini_wrong_dimension_or_bad_response_is_an_error() -> None:
    seen: list[httpx.Request] = []
    e = GeminiEmbedder("k" * 30, "m", 4, client=_client([_ok(1, 3)], seen), sleep=lambda s: None)
    with pytest.raises(EmbeddingError, match="차원"):
        e.embed(["x\ny"])
    e2 = GeminiEmbedder(
        "k" * 30,
        "m",
        4,
        client=_client([httpx.Response(400, text="bad")], seen),
        sleep=lambda s: None,
    )
    with pytest.raises(EmbeddingError) as exc:
        e2.embed(["x\ny"])
    assert exc.value.kind == "config_or_bug"


def test_as_document_format() -> None:
    assert as_document("a.md > 제목\n본문\n둘째 줄") == "title: a.md > 제목 | text: 본문\n둘째 줄"


def test_unchanged_file_is_not_rewritten(docs: Path) -> None:
    """두 번째 실행이 바뀌지 않은 파일을 지웠다 다시 쓰지 않는다(만든 시각이 그대로)."""
    from datetime import timedelta

    db = Database()
    index_documents(db, _source(docs), FakeEmbedder(), size=500, overlap=80, now=NOW)
    later = NOW + timedelta(hours=1)
    index_documents(db, _source(docs), FakeEmbedder(), size=500, overlap=80, now=later)
    created = {r[0] for r in db.execute("SELECT created_at FROM doc_chunks")}
    assert created == {NOW.replace(tzinfo=None)}


def test_vector_literal_round_trips_through_float_array() -> None:
    from nsm.rag.indexer import vector_literal

    db = Database()
    v = [0.5, -0.25, 1e-7]
    got = db.execute("SELECT ?::FLOAT[]", [vector_literal(v)])[0][0]
    assert got == pytest.approx(v, rel=1e-6)
    assert vector_literal(None) is None


def test_gemini_429_waits_the_retry_delay_from_the_response() -> None:
    """무료 구간 429 본문의 RetryInfo(예: 47s)만큼 기다렸다 다시(2026-10-30 실제 응답 모양)."""
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "47s"}
    limited = httpx.Response(429, json={"error": {"details": [info]}})
    e = GeminiEmbedder(
        "k" * 30,
        "gemini-embedding-2",
        4,
        client=_client([limited, _ok(1, 4)], seen),
        sleep=sleeps.append,
    )
    assert len(e.embed(["x\ny"])) == 1
    assert sum(sleeps) == 48.0  # 1초씩 나눠 기다림(끄는 중이면 끊으려고)


def test_gemini_daily_quota_stops_at_once_without_retrying() -> None:
    """하루 한도(실제 응답 모양)면 다시 보내지 않는다: 막힌 시도도 한도에 세어져 남은 한도를 태운다."""
    seen: list[httpx.Request] = []
    sleeps: list[float] = []
    quota = {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [
            {
                "quotaId": "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier",
                "quotaValue": "1000",
            }
        ],
    }
    info = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "50905s"}
    daily = httpx.Response(429, json={"error": {"details": [quota, info]}})
    now = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
    e = GeminiEmbedder(
        "k" * 30,
        "gemini-embedding-2",
        4,
        client=_client([daily], seen),
        sleep=sleeps.append,
        now=lambda: now,
    )
    with pytest.raises(EmbeddingError, match="하루 한도") as exc:
        e.embed(["x"])
    assert len(seen) == 1 and sleeps == []
    assert exc.value.kind == "rate_limited"
    assert (exc.value.retry_at - now).total_seconds() == 50905


def test_max_embed_per_run_defers_the_rest(docs: Path) -> None:
    db = Database()
    emb = FakeEmbedder()
    rep = _index(db, docs, emb, max_embed=1)
    assert rep.embedded == 1 and rep.deferred == 1 and rep.without_vector == 1
    rep = _index(db, docs, FakeEmbedder(), max_embed=1)
    assert rep.embedded == 1 and rep.deferred == 0 and rep.without_vector == 0


# ------------------------------------------------------------------ 리뷰 반영


def test_dimension_change_re_embeds_even_with_same_model(docs: Path) -> None:
    db = Database()
    _index(db, docs, FakeEmbedder(dim=8))
    wider = FakeEmbedder(dim=16)
    rep = _index(db, docs, wider)
    assert sum(len(c) for c in wider.calls) == 2 and rep.reused == 0
    assert {r[0] for r in db.execute("SELECT len(embedding) FROM doc_chunks")} == {16}


def test_empty_source_does_not_wipe_the_index(docs: Path, tmp_path: Path) -> None:
    from nsm.rag.indexer import EmptySourceError

    db = Database()
    _index(db, docs, FakeEmbedder())
    empty = tmp_path / "typo-docs"
    empty.mkdir()
    with pytest.raises(EmptySourceError):
        _index(db, empty, FakeEmbedder())
    assert db.execute("SELECT count(*) FROM doc_chunks")[0][0] == 2


def test_unchanged_file_with_missing_vectors_is_filled_in_place(docs: Path) -> None:
    from datetime import timedelta

    db = Database()
    _index(db, docs, None)  # 키 없이 조각만
    index_documents(
        db, _source(docs), FakeEmbedder(), size=500, overlap=80, now=NOW + timedelta(hours=1)
    )
    rows = db.execute("SELECT created_at, embedding IS NOT NULL FROM doc_chunks")
    assert all(ok for _, ok in rows)
    assert {c for c, _ in rows} == {NOW.replace(tzinfo=None)}  # 다시 쓰지 않고 그 자리에서 채움


def test_include_and_exclude_follow_the_same_root_anchored_rules(tmp_path: Path) -> None:
    from nsm.rag.sources import matches

    assert matches("samples/a.md", "samples/") and not matches("devlog/samples/a.md", "samples/")
    assert matches("adr/0001.md", "adr/") and not matches("old/adr/0001.md", "adr/")
    assert matches("README.md", "*.md") and not matches("adr/0001.md", "*.md")
    assert matches("adr/0000-template.md", "adr/0000-template.md")
