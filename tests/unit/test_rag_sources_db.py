"""R4: DB 출처 어댑터(수집 자료·우리 글, ADR 0065). 임시 DuckDB에 행을 넣고 문서·조각 모양을 본다. 네트워크 없음."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from nsm.rag.indexer import chunk_source, index_documents
from nsm.rag.sources_db import (
    CollectedSource,
    PostsSource,
    _cards_markdown,
    card_lines,
    post_title,
)
from nsm.store.db import Database, to_db
from tests.fakes import FakeEmbedder

NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)
FIX = Path(__file__).resolve().parents[1] / "fixtures"


def add_item(
    db: Database,
    iid: str,
    *,
    relevance: float | None = 0.9,
    days_ago: float = 1,
    text: str = "치이카와 극장판이 250만 관객을 넘었다.",
    title: str = "치이카와 250만",
    now: datetime | None = None,
) -> None:
    db.execute(
        "INSERT INTO source_items (id, source, url, url_hash, title, text, collected_at, relevance, topic) "
        "VALUES (?, 'naver', ?, ?, ?, ?, ?, ?, '치이카와 극장판')",
        [
            iid,
            f"https://example.com/{iid}",
            f"h-{iid}",
            title,
            text,
            to_db((now or NOW) - timedelta(days=days_ago)),
            relevance,
        ],
    )


def generated(body: str = "하치와레 이름은 털 무늬에서 왔대요. 여러분은 알았나요?") -> str:
    return json.dumps(
        {
            "topic": "하치와레",
            "template": "list",
            "deck": {
                "slides": [
                    {"kind": "cover", "title": "하치와레\n이름 뜻", "subtitle": "털 무늬 이야기"},
                    {"kind": "list", "heading": "이름 뜻", "bullets": ["八처럼 갈라진 무늬"]},
                    {"kind": "outro", "cta": "알고 있었나요?", "sources": ["https://x.example/a"]},
                ]
            },
            "instagram_caption": "인스타 캡션: " + body,
            "threads_text": body,
            "blog_title": "",
            "blog_markdown": "",
            "claims": [],
        },
        ensure_ascii=False,
    )


def add_post(
    db: Database,
    cid: str,
    platform: str = "threads",
    *,
    status: str = "published",
    ext: str | None = "EXT1",
    body: str | None = None,
    now: datetime | None = None,
) -> None:
    t = to_db((now or NOW) - timedelta(days=2))
    if not db.execute("SELECT 1 FROM contents WHERE id = ?", [cid]):
        db.execute(
            "INSERT INTO contents (id, status, topic, template, generated, relevance_confidence, "
            "created_at, updated_at) VALUES (?, 'published', '하치와레', 'list', ?, 0.9, ?, ?)",
            [cid, generated(body) if body else generated(), t, t],
        )
    db.execute(
        "INSERT INTO publications (id, content_id, platform, status, external_id, url, published_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            f"{cid}-{platform}",
            cid,
            platform,
            status,
            ext,
            f"https://www.threads.com/@nonsleepmarketer/post/{ext}" if ext else None,
            t if status == "published" else None,
            t,
        ],
    )


def add_comment(
    db: Database,
    cid: str,
    ext: str,
    text: str,
    reply: str | None,
    *,
    platform: str = "threads",
    status: str = "auto_replied",
    author: str = "누군가의이름",
    category: str | None = None,
    reply_ext: str | None = None,
) -> None:
    t = to_db(NOW - timedelta(days=1))
    db.execute(
        "INSERT INTO comments (id, platform, external_id, post_external_id, author_id, author_name, text, "
        "created_at, fetched_at, status, reply_text, category, reply_external_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            f"{platform}:{cid}",
            platform,
            cid,
            ext,
            "author-id-123",
            author,
            text,
            t,
            t,
            status,
            reply,
            category,
            reply_ext,
        ],
    )


def _platform_doc(db: Database):
    """카드 문서(posts/<cid>/cards)를 뺀 플랫폼 문서 하나."""
    docs = PostsSource(db, tz="Asia/Seoul").documents()
    [doc] = [d for d in docs if not d.path.endswith("/cards")]
    return doc


def _collected(db: Database, **kw) -> CollectedSource:
    return CollectedSource(
        db,
        min_relevance=kw.get("min_relevance", 0.8),
        days=kw.get("days", 180),
        now=kw.get("now", NOW),
        tz="Asia/Seoul",
    )


# ------------------------------------------------------------------ (1) 수집 자료


def test_collected_filters_by_relevance_period_and_empty_text(db: Database) -> None:
    add_item(db, "A")  # 들어감
    add_item(db, "B", relevance=0.79)  # 관련성 기준 아래
    add_item(db, "C", relevance=None)  # 분류 전
    add_item(db, "D", days_ago=181)  # 기간 밖
    add_item(db, "E", text="   ")  # 본문 없음
    add_item(db, "F", relevance=0.8, days_ago=179)  # 경계 안
    paths = [d.path for d in _collected(db).documents()]
    assert paths == ["source_items/A", "source_items/F"]


def test_collected_markdown_and_heading_path(db: Database) -> None:
    add_item(db, "A", text="첫 문단.\n# 원문 속 제목 줄\n둘째 문단.")
    [doc] = _collected(db).documents()
    lines = doc.text.splitlines()
    assert lines[0] == "# 치이카와 250만"
    assert lines[1] == "출처: naver https://example.com/A"
    assert lines[2].startswith("수집: 2026-11-0") and lines[3] == "주제: 치이카와 극장판"
    assert lines[4] == "" and lines[5] == "첫 문단."
    assert "\\# 원문 속 제목 줄" in doc.text  # 원문 속 '# '는 제목으로 읽히지 않는다
    assert doc.label == "수집 자료"
    chunks = chunk_source(_collected(db), 500, 80)["source_items/A"]
    assert {c.heading_path for c in chunks} == {"수집 자료 > 치이카와 250만"}
    assert chunks[0].text.startswith(
        "수집 자료 > 치이카와 250만\n출처: naver https://example.com/A"
    )


def test_long_collected_text_is_split_by_existing_rules(db: Database) -> None:
    add_item(db, "A", text="\n\n".join(f"{i}번째 문단은 극장판 소식이다. " * 6 for i in range(20)))
    chunks = chunk_source(_collected(db), 500, 80)["source_items/A"]
    assert len(chunks) > 3 and all(len(c.text) <= 500 + 80 + 40 for c in chunks)


# ------------------------------------------------------------------ (2) 우리 글


def test_posts_only_published_with_platform_body_and_cards(db: Database) -> None:
    add_post(db, "C1", "threads")
    add_post(db, "C1", "instagram", ext="IG1")
    add_post(db, "C2", "threads", status="queued", ext=None)  # 아직 안 올림
    add_post(db, "C3", "threads", status="failed", ext=None)
    docs = {d.path: d for d in PostsSource(db, tz="Asia/Seoul").documents()}
    assert sorted(docs) == ["posts/C1/cards", "posts/C1/instagram", "posts/C1/threads"]
    th = docs["posts/C1/threads"].text
    assert th.splitlines()[:5] == [
        "# 하치와레 이름 뜻",
        "플랫폼: threads",
        "게시: 2026-11-04",
        "주소: https://www.threads.com/@nonsleepmarketer/post/EXT1",
        "주제: 하치와레",
    ]
    assert "하치와레 이름은 털 무늬에서 왔대요." in th and "인스타 캡션" not in th
    assert "인스타 캡션: 하치와레" in docs["posts/C1/instagram"].text  # 플랫폼에 맞는 본문
    # 카드 문구는 플랫폼 문서에 없고 원고당 카드 문서 하나에만 있다(R4c)
    for platform in ("threads", "instagram"):
        assert "## 카드 문구" not in docs[f"posts/C1/{platform}"].text
    cards = docs["posts/C1/cards"].text
    assert "- 八처럼 갈라진 무늬" in cards and "- 알고 있었나요?" in cards
    assert "https://x.example/a" not in cards  # 카드의 출처 주소 목록은 싣지 않는다
    assert docs["posts/C1/cards"].label == "우리 글"
    chunks = chunk_source(PostsSource(db, tz="Asia/Seoul"), 500, 80)
    assert {c.heading_path for c in chunks["posts/C1/threads"]} == {"우리 글 > 하치와레 이름 뜻"}


def test_post_comments_without_author_names(db: Database) -> None:
    add_post(db, "C1", "threads", ext="EXT1")
    add_post(db, "C1", "instagram", ext="IG1")
    add_comment(db, "c1", "EXT1", "아직 안봤어요", "천천히 보셔도 돼요!")
    add_comment(db, "c2", "EXT1", "😢", None, status="resolved")
    add_comment(db, "c3", "EXT1", "광고 링크", None, status="hidden")  # 숨긴 댓글은 싣지 않는다
    add_comment(db, "c4", "IG1", "인스타 댓글", "고마워요", platform="instagram")
    add_comment(db, "c5", "EXT1", "다른 플랫폼 같은 id", "x", platform="instagram")
    docs = {d.path: d.text for d in PostsSource(db, tz="Asia/Seoul").documents()}
    th = docs["posts/C1/threads"]
    assert "## 댓글\n- 아직 안봤어요 → 답글: 천천히 보셔도 돼요!\n- 😢\n" in th
    assert "광고 링크" not in th and "인스타 댓글" not in th and "다른 플랫폼" not in th
    assert "- 인스타 댓글 → 답글: 고마워요" in docs["posts/C1/instagram"]
    for text in docs.values():
        assert "누군가의이름" not in text and "author-id-123" not in text
    heads = {
        c.heading_path
        for c in chunk_source(PostsSource(db, tz="Asia/Seoul"), 500, 80)["posts/C1/threads"]
    }
    assert "우리 글 > 하치와레 이름 뜻 > 댓글" in heads


def test_post_without_external_id_has_no_comment_section(db: Database) -> None:
    add_post(db, "C1", "threads", ext=None)
    add_comment(db, "c1", "", "엉뚱한 댓글", None)
    doc = _platform_doc(db)
    assert "## 댓글" not in doc.text and "주소: 없음" in doc.text


def test_title_falls_back_to_first_sentence_and_cards_cover_all_kinds() -> None:
    gen = {"deck": {"slides": []}, "blog_title": ""}
    assert post_title(gen, "첫 문장이에요! 둘째 문장이에요.") == "첫 문장이에요!"
    assert post_title({"blog_title": "블로그 제목"}, "본문") == "블로그 제목"
    assert post_title({}, "") == "제목 없음"
    lines = card_lines(
        {
            "slides": [
                {"kind": "body", "heading": "본문 제목", "body": "본문", "bullets": ["점"]},
                {
                    "kind": "compare",
                    "heading": "비교",
                    "left": {"title": "왼", "points": ["a", "b"]},
                    "right": {"title": "오", "points": ["c"]},
                },
            ]
        }
    )
    assert lines == ["본문 제목", "본문", "점", "비교", "왼: a, b", "오: c"]


def test_broken_generated_json_still_gives_a_document(db: Database) -> None:
    add_post(db, "C1", "threads")
    db.execute("UPDATE contents SET generated = '{not json' WHERE id = 'C1'")
    doc = _platform_doc(db)
    assert doc.text.startswith("# 제목 없음\n플랫폼: threads")


# ------------------------------------------------------------------ (4) 기간 밖으로 밀린 자료


def test_items_pushed_out_of_the_period_are_deleted_from_the_index(db: Database) -> None:
    add_item(db, "A", days_ago=170)
    add_item(db, "B", days_ago=1)
    emb = FakeEmbedder()
    first = index_documents(db, _collected(db), emb, size=500, overlap=80, now=NOW)
    assert first.files == 2
    later = NOW + timedelta(days=15)  # A는 185일 전이 됨
    rep = index_documents(db, _collected(db, now=later), emb, size=500, overlap=80, now=later)
    assert rep.deleted_files == ["source_items/A"]
    paths = {r[0] for r in db.execute("SELECT DISTINCT path FROM doc_chunks")}
    assert paths == {"source_items/B"}
    # 전부 밀려나도(0건) "경로 실수"로 멈추지 않고 지운다(DB 출처는 0건이 정상)
    gone = NOW + timedelta(days=400)
    rep = index_documents(db, _collected(db, now=gone), emb, size=500, overlap=80, now=gone)
    assert rep.deleted_files == ["source_items/B"]
    assert db.execute("SELECT count(*) FROM doc_chunks")[0][0] == 0


def test_measurement_questions_file_shape() -> None:
    """출처 측정 질문(질문 5개)은 문서 질문 파일과 따로 있고, 출처·정답 경로를 가진다."""
    qs = yaml.safe_load((FIX / "rag" / "questions_sources.yaml").read_text("utf-8"))["questions"]
    assert [q["n"] for q in qs] == [1, 2, 3, 4, 5]
    for q in qs:
        assert q["q"].strip() and q["kind"] in ("collected", "posts", "any")
        assert q["expected"] and all(
            e.startswith(("source_items/", "posts/")) for e in q["expected"]
        )
    docs_qs = yaml.safe_load((FIX / "rag" / "questions.yaml").read_text("utf-8"))["questions"]
    assert {q["q"] for q in qs}.isdisjoint({q["q"] for q in docs_qs})


# ------------------------------------------------------------------ R4 리뷰: 사람 표시·코드 울타리


def test_handles_and_author_name_are_scrubbed_from_comments_and_replies(db: Database) -> None:
    add_post(db, "C1", "threads", ext="EXT1")
    add_comment(
        db,
        "c1",
        "EXT1",
        "@friend_99 이거 봐 @other.person",
        "누군가의이름님 고마워요! @friend_99님도 반가워요",
    )
    doc = _platform_doc(db)
    assert "friend_99" not in doc.text and "other.person" not in doc.text
    assert "누군가의이름" not in doc.text
    assert "- @… 이거 봐 @… → 답글: …님 고마워요! @…님도 반가워요" in doc.text


def test_unclosed_code_fence_in_source_text_does_not_swallow_later_sections(db: Database) -> None:
    add_post(db, "C1", "threads", body="본문 시작\n```\n닫히지 않은 울타리")
    add_comment(db, "c1", "EXT1", "댓글이에요", "답글이에요")
    heads = {
        c.heading_path
        for c in chunk_source(PostsSource(db, tz="Asia/Seoul"), 500, 80)["posts/C1/threads"]
    }
    assert "우리 글 > 하치와레 이름 뜻 > 댓글" in heads
    cards = chunk_source(PostsSource(db, tz="Asia/Seoul"), 500, 80)["posts/C1/cards"]
    assert {c.heading_path for c in cards} == {"우리 글 > 하치와레 이름 뜻 > 카드 문구"}
    add_item(db, "A", text="기사\n~~~\n# 제목처럼 보이는 줄")
    [doc] = _collected(db).documents()
    assert "\~~~" in doc.text and "\# 제목처럼" in doc.text


# ------------------------------------------------------------------ 댓글·답글 경계: 스팸, 보내지 않은 초안, 빈 글 id


def test_instagram_spam_kept_as_ignored_is_excluded(db: Database) -> None:
    add_post(db, "C1", "instagram", ext="IG1")
    add_comment(
        db,
        "c1",
        "IG1",
        "광고 링크 눌러요",
        None,
        platform="instagram",
        status="ignored",
        category="spam",
    )
    add_comment(
        db,
        "c2",
        "IG1",
        "정상 댓글(하루 한도)",
        None,
        platform="instagram",
        status="ignored",
        category="reaction",
    )
    doc = _platform_doc(db)
    assert "광고 링크" not in doc.text
    assert "- 정상 댓글(하루 한도)" in doc.text


def test_only_replies_that_were_sent_are_attached(db: Database) -> None:
    add_post(db, "C1", "threads", ext="EXT1")
    add_comment(db, "c1", "EXT1", "금칙어 걸린 댓글", "보내지 않은 초안", status="escalated")
    add_comment(db, "c2", "EXT1", "처리 완료만 누른 댓글", "남은 초안", status="resolved")
    add_comment(
        db,
        "c3",
        "EXT1",
        "사람이 답한 댓글",
        "사람이 보낸 답글",
        status="resolved",
        reply_ext="seen:c1",
    )
    add_comment(db, "c4", "EXT1", "전송 중 댓글", "전송 중 초안", status="replying")
    add_comment(db, "c5", "EXT1", "자동 답글 댓글", "자동으로 보낸 답글")
    doc = _platform_doc(db)
    for line in ("- 금칙어 걸린 댓글\n", "- 처리 완료만 누른 댓글\n", "- 전송 중 댓글\n"):
        assert line in doc.text  # 댓글 본문은 싣고 답글만 뺀다
    for draft in ("보내지 않은 초안", "남은 초안", "전송 중 초안"):
        assert draft not in doc.text
    assert "- 사람이 답한 댓글 → 답글: 사람이 보낸 답글" in doc.text
    assert "- 자동 답글 댓글 → 답글: 자동으로 보낸 답글" in doc.text


def test_empty_external_id_does_not_match_empty_post_ids(db: Database) -> None:
    add_post(db, "C1", "threads", ext="")
    add_comment(db, "c1", "", "글 id가 빈 댓글", "답글")
    doc = _platform_doc(db)
    assert "## 댓글" not in doc.text and "글 id가 빈 댓글" not in doc.text


# ------------------------------------------------------------------ R4c: 카드 문구는 원고당 한 번

CARD_LINES = (
    "- 하치와레 이름 뜻\n- 털 무늬 이야기\n- 이름 뜻\n- 八처럼 갈라진 무늬\n- 알고 있었나요?"
)


def _cards_docs(db: Database) -> dict[str, str]:
    docs = PostsSource(db, tz="Asia/Seoul").documents()
    return {d.path: d.text for d in docs if d.path.endswith("/cards")}


def test_card_document_once_even_when_posted_on_two_platforms(db: Database) -> None:
    add_post(db, "C1", "threads")
    add_post(db, "C1", "instagram", ext="IG1")
    add_post(db, "C2", "instagram", ext="IG2")  # 한 플랫폼만 올라도 카드 문서는 하나
    assert sorted(_cards_docs(db)) == ["posts/C1/cards", "posts/C2/cards"]


def test_no_card_document_without_deck_or_without_published_post(db: Database) -> None:
    add_post(db, "C1", "threads")
    no_deck = json.dumps({"threads_text": "본문"})
    db.execute("UPDATE contents SET generated = ? WHERE id = 'C1'", [no_deck])
    add_post(db, "C2", "threads", status="queued", ext=None)
    add_post(db, "C3", "threads", status="failed", ext=None)
    assert _cards_docs(db) == {}
    assert [d.path for d in PostsSource(db, tz="Asia/Seoul").documents()] == ["posts/C1/threads"]


def test_card_document_has_only_cards_section_and_matches_old_card_chunk(db: Database) -> None:
    add_post(db, "C1", "threads", ext="EXT1")
    add_post(db, "C1", "instagram", ext="IG1")
    add_comment(db, "c1", "EXT1", "댓글이에요", "답글이에요")
    [cards] = _cards_docs(db).values()
    assert cards == "# 하치와레 이름 뜻\n\n## 카드 문구\n" + CARD_LINES + "\n"
    for leak in ("댓글", "답글이에요", "털 무늬에서 왔대요", "인스타 캡션", "플랫폼:", "주소:"):
        assert leak not in cards
    chunks = chunk_source(PostsSource(db, tz="Asia/Seoul"), 500, 80)["posts/C1/cards"]
    # 예전 플랫폼 문서의 카드 조각과 같은 글(같은 제목 경로 + 같은 줄): 점수 비교가 그대로 된다
    assert [c.text for c in chunks] == ["우리 글 > 하치와레 이름 뜻 > 카드 문구\n" + CARD_LINES]


def test_card_title_does_not_depend_on_platform_body() -> None:
    gen = {"deck": {"slides": [{"kind": "outro", "cta": "끝"}]}, "threads_text": "스레드 첫 문장."}
    # 표지·블로그 제목이 없으면 본문 첫 문장 대신 "제목 없음"(플랫폼마다 달라지지 않게)
    assert _cards_markdown(gen).startswith("# 제목 없음\n")


def test_index_has_one_card_chunk_per_content(db: Database) -> None:
    add_post(db, "C1", "threads", ext="EXT1")
    add_post(db, "C1", "instagram", ext="IG1")
    add_comment(db, "c1", "EXT1", "댓글이에요", "답글이에요")
    emb = FakeEmbedder()
    index_documents(db, PostsSource(db, tz="Asia/Seoul"), emb, size=500, overlap=80, now=NOW)
    rows = db.execute(
        "SELECT path, heading_path FROM doc_chunks WHERE source_kind = 'posts' ORDER BY path, seq"
    )
    assert [r for r in rows if r[1].endswith("카드 문구")] == [
        ("posts/C1/cards", "우리 글 > 하치와레 이름 뜻 > 카드 문구")
    ]
    assert len(rows) == 4  # 스레드 본문·댓글, 인스타 본문, 카드
