"""R7: 시간 질문("가장 최근 댓글", "어제 올린 글")에 붙는 최근 활동 조각(ADR 0071). 임시 DuckDB, 네트워크·키 없음."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nsm.rag.answer import (
    RECENT_PATH,
    RECENT_TOP,
    SUMMARY_PATH,
    SYSTEM,
    TIME_WORDS,
    QaResult,
    recent_activity,
    save_qa,
    wants_recent,
    wants_summary,
)
from nsm.rag.search import Hit
from nsm.store.db import Database, to_db
from nsm.store.repo import Repository
from tests.fakes import FakeEmbedder, FixedClock
from tests.unit.test_rag_post_metrics import add_metrics
from tests.unit.test_rag_sources_db import add_post

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)  # 11-12 12:00 KST
TZ = "Asia/Seoul"


def _repo(db: Database) -> Repository:
    return Repository(db, FixedClock(NOW))


def post(db: Database, cid: str, platform: str, ext: str, at: datetime) -> None:
    add_post(db, cid, platform, ext=ext, now=NOW)
    db.execute(  # 제목이 원고마다 다르게(블로그 제목 → post_title이 고름)
        "UPDATE contents SET generated = ? WHERE id = ?",
        [json.dumps({"blog_title": f"글 {cid}", "threads_text": "본문"}), cid],
    )
    db.execute(
        "UPDATE publications SET published_at = ? WHERE id = ?", [to_db(at), f"{cid}-{platform}"]
    )


def comment(
    db: Database,
    cid: str,
    ext: str,
    at: datetime,
    text: str,
    reply: str | None = None,
    *,
    platform: str = "threads",
    status: str = "auto_replied",
    category: str | None = None,
    reply_ext: str | None = None,
    author: str = "홍길동이름",
) -> None:
    db.execute(
        "INSERT INTO comments (id, platform, external_id, post_external_id, author_id, author_name, text, "
        "created_at, fetched_at, status, reply_text, category, reply_external_id) "
        "VALUES (?, ?, ?, ?, 'author-id-1', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            f"{platform}:{cid}",
            platform,
            cid,
            ext,
            author,
            text,
            to_db(at),
            to_db(at),
            status,
            reply,
            category,
            reply_ext,
        ],
    )


def _section(text: str, name: str) -> list[str]:
    """ "최근 게시 글:"·"최근 댓글:" 아래 "- " 줄들."""
    lines = text.split("\n")
    i = lines.index(f"{name}:")
    out = []
    for ln in lines[i + 1 :]:
        if not ln.startswith("- "):
            break
        out.append(ln)
    return out


# ------------------------------------------------------------------ 언제 붙나


@pytest.mark.parametrize(
    "q", ["가장 최근에 달린 댓글이 뭐야", "어제 올린 글", "마지막으로 답한 댓글", "지난주 글"]
)
def test_time_words_trigger_recent(q: str) -> None:
    assert wants_recent(q, None) and wants_recent(q, "") and wants_recent(q, "posts")


def test_no_time_word_or_other_source_never_triggers() -> None:
    assert not wants_recent("보호 모드가 뭐야", None)
    assert not wants_recent("가장 최근 댓글", "docs")
    assert not wants_recent("어제 올린 글", "collected")
    # 시간 낱말만 있고 글·댓글 활동을 묻지 않으면 붙이지 않는다(문서 질문에 Claude 비용이 들지 않게, R7 리뷰)
    assert not wants_recent("오늘 질문 수 상한은 몇 개야", None)
    assert not wants_recent("최근 며칠 수집 자료까지 찾아", None)
    assert not wants_recent("마지막 실행 시각은 어디서 봐", None)
    assert "가장" not in TIME_WORDS  # 순위 낱말은 RANK_WORDS에 그대로
    q = "가장 최근 댓글"
    assert wants_recent(q, None) and wants_summary(q, None)  # 둘 다 붙어도 된다


def test_system_prompt_has_time_and_placeholder_rules() -> None:
    assert "날짜가 없는 조각으로 순서를 추측하지 않습니다" in SYSTEM
    assert "자리표시자는 그대로 옮기지 말고" in SYSTEM


# ------------------------------------------------------------------ 조각 내용


def test_recent_activity_newest_first_with_local_time(db: Database) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=3))
    post(db, "B", "instagram", "i1", datetime(2026, 11, 11, 15, 30, tzinfo=UTC))  # 11-12 00:30 KST
    comment(db, "c1", "t1", NOW - timedelta(days=2), "옛 댓글", "옛 답글")
    comment(
        db, "c2", "i1", datetime(2026, 11, 11, 16, 5, tzinfo=UTC), "새 댓글", platform="instagram"
    )
    hit = recent_activity(_repo(db), NOW, TZ)
    assert hit is not None and hit.path == RECENT_PATH and hit.source_kind == "posts"
    assert hit.heading_path == "우리 글 > 최근 활동(11-12 기준)" and hit.score == 0.0
    assert _section(hit.text, "최근 게시 글") == [
        "- 11-12 00:30 게시 | 인스타그램 | 글 B",  # UTC 11-11 15:30이 아니라 서울 시각
        "- 11-09 12:00 게시 | 스레드 | 글 A",
    ]
    assert _section(hit.text, "최근 댓글") == [
        "- 11-12 01:05 | 인스타그램 | 글 B | 댓글: 새 댓글 → 답글: 없음",
        "- 11-10 12:00 | 스레드 | 글 A | 댓글: 옛 댓글 → 답글: 옛 답글",
    ]


def test_recent_activity_skips_hidden_spam_and_unsent_drafts(db: Database) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    base = NOW - timedelta(hours=5)
    comment(db, "h", "t1", base, "가려 둔 글귀", status="hidden")
    comment(db, "s", "t1", base, "광고 글귀", status="ignored", category="spam")
    comment(db, "e", "t1", base, "금칙어 댓글", "보내지 않은 초안", status="escalated")
    comment(db, "r", "t1", base, "처리만 누름", "남은 초안", status="resolved")
    comment(db, "m", "t1", base, "사람이 답함", "보낸 답글", status="resolved", reply_ext="R1")
    text = recent_activity(_repo(db), NOW, TZ).text
    for leak in ("가려 둔 글귀", "광고 글귀", "보내지 않은 초안", "남은 초안"):
        assert leak not in text
    assert "댓글: 금칙어 댓글 → 답글: 없음" in text and "댓글: 처리만 누름 → 답글: 없음" in text
    assert "댓글: 사람이 답함 → 답글: 보낸 답글" in text


def test_recent_activity_scrubs_names_and_cuts_long_lines(db: Database) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    comment(
        db, "c", "t1", NOW, "@friend_9 " + "가" * 200, "홍길동이름님 고마워요", author="홍길동이름"
    )
    (line,) = _section(recent_activity(_repo(db), NOW, TZ).text, "최근 댓글")
    assert "홍길동이름" not in line and "friend_9" not in line and "author-id" not in line
    assert "답글: …님 고마워요" in line
    body = line.split("댓글: ", 1)[1].split(" → ")[0]
    assert len(body) == 120 and body.endswith("…")


def test_recent_activity_limits_and_only_comments_on_published_posts(db: Database) -> None:
    """지운·실패한 글의 댓글은 10자리를 차지하지 않는다(R7 리뷰)."""
    for i in range(RECENT_TOP + 2):
        post(db, f"P{i:02d}", "threads", f"t{i}", NOW - timedelta(hours=i + 1))
        comment(db, f"c{i}", f"t{i}", NOW - timedelta(minutes=i + 1), f"댓글{i}")
    comment(db, "orphan", "gone", NOW, "지운 글의 댓글")
    add_post(db, "F", "threads", status="failed", ext="tf", now=NOW)
    comment(db, "failed", "tf", NOW, "실패한 글의 댓글")
    db.execute("UPDATE publications SET external_id = '' WHERE id = 'P11-threads'")
    comment(db, "empty", "", NOW, "글 id가 빈 댓글")
    hit = recent_activity(_repo(db), NOW, TZ)
    posts, comments = _section(hit.text, "최근 게시 글"), _section(hit.text, "최근 댓글")
    assert len(posts) == RECENT_TOP and posts[0].endswith("| 글 P00")
    assert len(comments) == RECENT_TOP and "| 글 P00 | 댓글: 댓글0" in comments[0]
    for leak in ("지운 글의", "실패한 글의", "글 id가 빈"):
        assert leak not in hit.text


def test_recent_activity_states_base_time_with_year(db: Database) -> None:
    """줄은 MM-DD HH:MM이라, 안내 줄의 연도 든 기준 시각으로 해 바뀜·"어제"를 가린다(R7 리뷰)."""
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    text = recent_activity(_repo(db), NOW, TZ).text
    assert "기준 시각 2026-11-12 12:00(Asia/Seoul)" in text.split("\n")[1]


def test_recent_activity_none_without_posts_or_comments(db: Database) -> None:
    assert recent_activity(_repo(db), NOW, TZ) is None
    add_post(db, "F", "threads", status="failed", ext="t1", now=NOW)  # 게시 안 된 글은 안 셈
    assert recent_activity(_repo(db), NOW, TZ) is None


def test_recent_activity_with_only_posts_says_no_comments(db: Database) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    text = recent_activity(_repo(db), NOW, TZ).text
    assert _section(text, "최근 댓글") == ["- 없음"]


# ------------------------------------------------------------------ 질문 한 번


class _Answerer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Hit], list[str] | None]] = []

    def answer(self, q, hits, notes=None):
        self.calls.append((q, hits, notes))
        return SimpleNamespace(
            text="가장 최근 댓글은 11-12에 달렸습니다 [1].", model="m", usd=0.001
        )


def _ask(db, app_config, q, answerer, **kw):
    from nsm.rag.answer import ask_docs

    app_config.settings.rag.min_score = 0.999  # 검색은 근거 없음
    return ask_docs(app_config, _repo(db), FakeEmbedder(), answerer, q, NOW, **kw)


def test_recent_alone_calls_claude_when_search_has_no_ground(db: Database, app_config) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    comment(db, "c", "t1", NOW - timedelta(hours=1), "언제 나와요")
    ans = _Answerer()
    res = _ask(db, app_config, "어제 올린 글", ans)
    assert len(ans.calls) == 1 and res.grounded
    _, hits, _ = ans.calls[0]
    assert [h.path for h in hits] == [RECENT_PATH]
    (src,) = res.sources
    assert src["summary"] is True and src["score"] is None and src["links"] == {}
    assert db.execute("SELECT count(*) FROM doc_chunks WHERE path = ?", [RECENT_PATH])[0][0] == 0


def test_summary_and_recent_together_are_both_marked(db: Database, app_config) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    add_metrics(db, "threads", "t1", NOW - timedelta(hours=2), views=300, likes=9)
    comment(db, "c", "t1", NOW - timedelta(hours=1), "최고예요")
    ans = _Answerer()
    res = _ask(db, app_config, "가장 최근에 달린 댓글", ans)
    assert [s["path"] for s in res.sources] == [SUMMARY_PATH, RECENT_PATH]
    for s in res.sources:
        assert s["score"] is None and s["summary"] is True
    stored = json.loads(db.execute("SELECT sources FROM doc_qa")[0][0])
    assert [(s["score"], s["summary"]) for s in stored] == [(None, True), (None, True)]


def test_docs_source_does_not_attach_recent(db: Database, app_config) -> None:
    post(db, "A", "threads", "t1", NOW - timedelta(days=1))
    ans = _Answerer()
    res = _ask(db, app_config, "어제 올린 글", ans, source_kind="docs")
    assert ans.calls == [] and res.sources == [] and not res.grounded


# ------------------------------------------------------------------ 최근 질문 카드 배지


def test_summary_ids_sees_recent_path(db: Database) -> None:
    from nsm.app.ask import summary_ids

    def qa(qid: str, path: str) -> None:
        src = [{"n": 1, "kind": "posts", "path": path, "heading": "h", "score": None, "text": path}]
        save_qa(db, QaResult(id=qid, question="q", answer="a", grounded=True, sources=src), NOW)

    qa("rec", RECENT_PATH)
    qa("sum", SUMMARY_PATH)
    qa("doc", "docs/guide.md")
    assert summary_ids(db, ["rec", "sum", "doc"]) == {"rec", "sum"}
    assert summary_ids(db, []) == set()
