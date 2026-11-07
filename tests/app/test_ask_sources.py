"""R4: 질문 화면의 출처 고르기(전체·문서·수집 자료·우리 글), 출처 배지, 출처별 조각 수, 예전 기록(kind 없음). 네트워크 없음."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from nsm.rag import budget
from nsm.rag.indexer import index_documents
from nsm.rag.sources_db import CollectedSource, PostsSource
from tests.app.test_ask_view import _no_network, docs, env  # noqa: F401  (픽스처)
from tests.unit.test_rag_sources_db import add_comment, add_item, add_post

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


@pytest.fixture
def senv(env):  # noqa: F811
    db = env["repo"].db
    add_item(db, "A", title="보호 모드 굿즈 소식", text="보호 모드 굿즈가 나왔다.", now=NOW)
    add_post(db, "C1", "threads", body="보호 모드 이야기를 우리 글로 올렸어요.", now=NOW)
    add_comment(db, "c1", "EXT1", "보호 모드 좋아요", "고마워요", author="남의이름")
    for src in (
        CollectedSource(db, min_relevance=0.8, days=180, now=NOW, tz="Asia/Seoul"),
        PostsSource(db, tz="Asia/Seoul"),
    ):
        index_documents(db, src, env["emb"], size=500, overlap=80, now=NOW)
    return env


def _ask(e, q: str, source: str | None = None):
    data = {"question": q, "csrf": e["state"].csrf_token}
    if source is not None:
        data["source"] = source
    return e["client"].post("/ask", data=data, follow_redirects=False)


@pytest.fixture
def seen(monkeypatch) -> list:
    calls: list = []
    real = budget.budgeted_search

    def spy(*a, **kw):
        calls.append(kw.get("source_kind"))
        return real(*a, **kw)

    monkeypatch.setattr(budget, "budgeted_search", spy)
    return calls


@pytest.mark.parametrize(
    ("picked", "kind"),
    [("", None), ("docs", "docs"), ("collected", "collected"), ("posts", "posts"), ("x", None)],
)
def test_picked_source_goes_to_search(senv, seen, picked, kind) -> None:
    r = _ask(senv, "보호 모드가 뭐야", picked)
    assert r.status_code == 303 and seen == [kind]
    if kind:
        assert f"&source={kind}#answer" in r.headers["location"]
    page = senv["client"].get(r.headers["location"]).text
    sel = re.search(r'<option value="([a-z]*)" selected>', page)
    assert sel and sel.group(1) == (kind or "")  # 다음 질문도 같은 출처로


def test_source_filter_limits_hits_and_badges(senv) -> None:
    r = _ask(senv, "보호 모드", "collected")
    page = senv["client"].get(r.headers["location"]).text
    assert "수집 자료 &gt; 보호 모드 굿즈 소식" in page
    assert 'class="badge gray src-kind">수집</span>' in page
    assert "adr/0008-guard.md" not in page.split('id="answer"')[1].split("검색 측정")[0]
    stored = json.loads(senv["repo"].db.execute("SELECT sources FROM doc_qa")[0][0])
    assert {s["kind"] for s in stored} == {"collected"}


def test_all_sources_show_each_badge(senv) -> None:
    senv["cfg"].settings.rag.search_k = 10
    r = _ask(senv, "보호 모드 이야기")
    page = senv["client"].get(r.headers["location"]).text
    for badge in ("문서", "수집", "우리 글"):
        assert f'class="badge gray src-kind">{badge}</span>' in page
    assert "남의이름" not in page  # 댓글 작성자 이름은 근거에 없다


def test_picker_and_per_source_counts_on_page(senv) -> None:
    page = senv["client"].get("/ask").text
    for value, name in [
        ("", "전체"),
        ("docs", "프로젝트 문서"),
        ("collected", "수집 자료"),
        ("posts", "우리가 올린 글"),
    ]:
        assert f'<option value="{value}"' in page and f">{name}</option>" in page
    assert '<option value="" selected>전체</option>' in page  # 기본은 전체
    st = re.search(r'class="muted small ask-stats">(.*?)</p>', page, re.S).group(1)
    assert "조각 6개" in st  # 문서 2 + 수집 1 + 우리 글 3(본문·카드 문구·댓글)
    assert re.search(r"문서</span> 2개", st) and re.search(r"수집</span> 1개", st)
    assert re.search(r"우리 글</span> 3개", st) and "밤 색인" in st


def test_old_records_without_kind_read_as_docs(senv) -> None:
    db = senv["repo"].db
    old = [
        {"n": 1, "path": "adr/0008-guard.md", "heading": "adr/0008-guard.md > 결정", "score": 0.7}
    ]
    db.execute(
        "INSERT INTO doc_qa VALUES ('old1', ?, '예전 질문', '예전 답 [1]', true, ?, '[]', 'm', 0.0, 10, NULL)",
        [datetime(2026, 10, 20, tzinfo=UTC).replace(tzinfo=None), json.dumps(old)],
    )
    page = senv["client"].get("/ask?qa=old1").text
    assert "예전 답" in page and 'class="badge gray src-kind">문서</span>' in page
    from nsm.rag.answer import get_qa

    assert get_qa(db, "old1")["sources"][0]["kind"] == "docs"
