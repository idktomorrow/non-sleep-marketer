"""R6: 문서 질문 화면 정리(ADR 0070). 순서·근거 제목·최근 질문 카드·예시 칩·출처 라디오·측정 접힘. 네트워크 없음."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from nsm.app.ask_format import source_title
from nsm.rag.answer import NO_GROUND_ANSWER
from nsm.rag.evaluate import MIN_NEEDLE, normalize
from tests.app.test_ask_sources import _put_qa, _src
from tests.app.test_ask_view import _ask, _no_network, docs, env  # noqa: F401  (픽스처)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "nsm/app/templates/ask.html"
LONG = "아주 긴 제목 " * 12  # 84자: 화면이 말줄임으로 가리고 글자는 그대로 둔다


@pytest.mark.parametrize(
    ("heading", "kind", "want"),
    [
        # 문서: 마지막 절 + 문서 제목(파일명은 펼쳤을 때 전체 경로로만)
        ("adr/0008-guard.md > 0008. 보호 모드 > 결정", "docs", ("결정", "0008. 보호 모드")),
        ("devlog/a.md > 배치 R3 > 실제 화면 > 400px", "docs", ("400px", "배치 R3")),
        ("adr/0008-guard.md > 0008. 보호 모드", "docs", ("0008. 보호 모드", "")),
        ("adr/0008-guard.md", "docs", ("adr/0008-guard.md", "")),
        (
            "adr/0008-guard.md > 0008. 보호 모드 > 결정",
            None,
            ("결정", "0008. 보호 모드"),
        ),  # 예전 기록
        # 수집 자료: 기사 제목만
        ("수집 자료 > 극장판 흥행 소식", "collected", ("극장판 흥행 소식", "")),
        # 우리 글: 글 제목과 절(카드 문구·댓글)만
        ("우리 글 > 하치와레 소개", "posts", ("하치와레 소개", "")),
        ("우리 글 > 하치와레 소개 > 카드 문구", "posts", ("하치와레 소개", "카드 문구")),
        ("우리 글 > 하치와레 소개 > 댓글", "posts", ("하치와레 소개", "댓글")),
        (
            "우리 글 > 성과 요약(최근 90일, 11-12 기준)",
            "posts",
            ("성과 요약(최근 90일, 11-12 기준)", ""),
        ),
        # 접두어("수집 자료"·"우리 글")가 없는 경로, 모르는 출처: 첫 칸 뒤 제목(없으면 그 칸)
        ("기사 제목만", "collected", ("기사 제목만", "")),
        ("새 출처 > 어떤 제목 > 절", "other", ("어떤 제목", "")),
        ("", "docs", ("제목 없음", "")),
        (None, "posts", ("제목 없음", "")),
    ],
)
def test_source_title_rules(heading, kind, want) -> None:
    assert source_title(heading, kind) == want


def test_source_title_keeps_long_titles_whole() -> None:
    title, sub = source_title(f"adr/x.md > {LONG} > {LONG}끝", "docs")
    assert len(title) > 60 and title.endswith("끝") and "…" not in title and sub == LONG.strip()


def test_long_title_is_shortened_by_css_not_cut(env) -> None:  # noqa: F811
    _put_qa(env["repo"].db, "long", [_src(kind="collected", heading=f"수집 자료 > {LONG}끝")])
    li = env["client"].get("/ask?qa=long").text.split('id="src-1"')[1].split("</li>")[0]
    assert f"{LONG}끝</span>" in li and f'title="{LONG}끝"' in li
    css = env["client"].get("/static/app.css").text
    rule = css[css.index(".src-title {") :].split("}")[0]
    assert "text-overflow: ellipsis" in rule and "60ch" in rule
    assert "word-break: break-all" not in css


def test_page_order_question_answer_recent_then_eval(env) -> None:  # noqa: F811
    html = _ask(env, "보호 모드가 뭐야").text
    order = [html.index(m) for m in ('id="ask-form"', 'id="answer"', 'id="recent"', 'id="eval"')]
    assert order == sorted(order)
    meta = re.search(r'<p class="hint ask-meta"([^>]*)>(.*?)</p>', html).groups()
    assert 'title="claude-haiku-5-5"' in meta[0] and "haiku" not in meta[1] and "초" in meta[1]


def test_eval_section_is_folded_unless_notice(env) -> None:  # noqa: F811
    assert '<details class="eval-section" id="eval">' in env["client"].get("/ask").text
    html = env["client"].get("/ask?eval=ok").text
    assert '<details class="eval-section" id="eval" open>' in html and "측정했습니다." in html
    js = env["client"].get("/static/ask.js").text
    assert '"#eval"' in js and "hashchange" in js and ".open = true" in js


def test_recent_questions_are_cards_with_preview_and_badges(env) -> None:  # noqa: F811
    _ask(env, "보호 모드가 뭐야")
    env["msgs"].text = "문서에 그 내용이 없습니다."
    _ask(env, "네이버 블로그는 왜 게시 안 해")
    html = env["client"].get("/ask").text
    recent = html[html.index('<h2 id="recent">') : html.index('id="eval"')]
    assert "<table" not in recent and recent.count('<li class="recent-card">') == 2
    cards = recent.split('<li class="recent-card">')[1:]
    newest, oldest = cards
    assert 'class="recent-preview">문서에 그 내용이 없습니다.</p>' in newest
    assert '<span class="badge">근거 없음</span>' in newest
    assert 'class="recent-preview">보호 모드는 처음 20건을' in oldest
    assert re.search(r'<span class="badge ok">근거 \d+개</span>', oldest)
    assert "약" in oldest and 'action="/ask/qa/' in oldest and "지우기" in oldest
    assert "전부 지우기" in recent.split("</h2>")[0]  # 제목 옆 그대로


def test_recent_card_shows_summary_badge_and_no_ground_text(env) -> None:  # noqa: F811
    db = env["repo"].db
    _put_qa(db, "sum", [_src(path="posts/_summary", heading="우리 글 > 성과 요약", score=None)])
    # 본문·제목에 같은 글이 있어도 path가 아니면 요약 배지가 붙지 않는다
    _put_qa(db, "txt", [_src(kind="docs", heading="posts/_summary", text='"posts/_summary"')])
    db.execute(
        "INSERT INTO doc_qa (id, asked_at, question, answer, grounded, sources) "
        "VALUES ('none', ?, '빈 답', '', false, '[]')",
        [datetime(2026, 11, 11, tzinfo=UTC).replace(tzinfo=None)],
    )
    html = env["client"].get("/ask").text
    recent = html[html.index('<h2 id="recent">') : html.index('id="eval"')]
    by_id = {
        c.split("?qa=")[1].split("#")[0]: c for c in recent.split('<li class="recent-card">')[1:]
    }
    assert 'class="badge src-summary">요약</span>' in by_id["sum"]
    assert "요약</span>" not in by_id["none"] and "요약</span>" not in by_id["txt"]
    assert f'class="recent-preview">{NO_GROUND_ANSWER}</p>' in by_id["none"]


def test_example_chips_are_buttons_and_source_radios_default_to_all(env) -> None:  # noqa: F811
    html = env["client"].get("/ask").text
    form = html.split('id="ask-form"')[1].split("</form>")[0]
    assert 'id="ask-q"' in form
    chips = re.findall(r'<button type="button" class="chip" data-example="([^"]+)"', form)
    assert len(chips) == 4 and form.count('class="chip"') == 4
    radios = re.findall(
        r'<input type="radio" id="(ask-source-[a-z]+)" name="source" value="([a-z]*)"( checked)?>',
        form,
    )
    assert [(i, v) for i, v, _ in radios] == [
        ("ask-source-all", ""),
        ("ask-source-docs", "docs"),
        ("ask-source-collected", "collected"),
        ("ask-source-posts", "posts"),
    ]
    assert [c for _, _, c in radios] == [" checked", "", "", ""]
    js = env["client"].get("/static/ask.js").text
    assert "button.chip[data-example]" in js and "focus()" in js


def _measurement_questions() -> list[str]:
    out = []
    for f in sorted((ROOT / "tests/fixtures/rag").glob("questions*.yaml")):
        out += [q["q"] for q in yaml.safe_load(f.read_text("utf-8"))["questions"]]
    return out


def test_template_does_not_quote_measurement_questions() -> None:
    """예시 칩·안내 문구에 측정 질문 원문을 쓰지 않는다(tests/unit/test_eval_questions_not_in_docs.py와 같은 규칙)."""
    qs = _measurement_questions()
    assert len(qs) >= 20
    text = normalize(TEMPLATE.read_text("utf-8"))
    bad = [q for q in qs if len(normalize(q)) >= MIN_NEEDLE and normalize(q) in text]
    assert not bad, bad
