"""주간 리포트 화면(C45): 리포트 마크다운을 읽는 문서로(md_lite)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from nsm.app.md_lite import report_html
from nsm.engine.weekly_report import build_report
from tests.app.test_dashboard import env  # noqa: F401  같은 대시보드 픽스처

SAMPLE = """# 주간 리포트 2026-11-02 ~ 2026-11-08

## 요약

- 게시 3건 (instagram 1, threads 2)
- 평균 참여율 4.2%

## 가장 잘된 콘텐츠

1. **하치와레 이름 뜻** (threads) 참여율 6.1%, 조회 420
   - 이유: 참여율이 평균의 1.5배, 정보형
2. **극장판 소식** (instagram) 참여율 3.0%, 조회 210
   - 이유: 리스트형
"""


def test_headings_bold_and_nested_lists() -> None:
    out = str(report_html(SAMPLE))
    assert "<h2>주간 리포트 2026-11-02 ~ 2026-11-08</h2>" in out  # # → h2(페이지 h1은 따로)
    assert "<h3>요약</h3>" in out and "<h3>가장 잘된 콘텐츠</h3>" in out
    assert "<ul><li>게시 3건 (instagram 1, threads 2)</li><li>평균 참여율 4.2%</li></ul>" in out
    assert "<strong>하치와레 이름 뜻</strong>" in out
    # 번호 목록 안에 들여쓴 하위 목록, 다음 번호에서 하위 목록이 닫힌다
    assert (
        "<ol><li><strong>하치와레 이름 뜻</strong> (threads) 참여율 6.1%, 조회 420"
        "<ul><li>이유: 참여율이 평균의 1.5배, 정보형</li></ul></li><li><strong>극장판 소식</strong>"
    ) in out
    assert out.endswith("</li></ul></li></ol>")
    for sym in ("# ", "**", "\n- "):
        assert sym not in out  # 기호가 글자로 남지 않는다


def test_script_and_angle_brackets_are_escaped() -> None:
    out = str(report_html("## <script>alert(1)</script>\n- a < b & **<i>x</i>**\n그냥 <b>글</b>"))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "a &lt; b &amp; <strong>&lt;i&gt;x&lt;/i&gt;</strong>" in out
    assert "<p>그냥 &lt;b&gt;글&lt;/b&gt;</p>" in out


def test_unknown_lines_become_paragraphs() -> None:
    out = str(report_html("첫 줄\n둘째 줄\n\n> 인용처럼 보이는 줄\n---"))
    assert "<p>첫 줄<br>둘째 줄</p>" in out
    assert "<p>&gt; 인용처럼 보이는 줄<br>---</p>" in out


def test_list_then_paragraph_closes_list_and_ol_start_kept() -> None:
    out = str(report_html("- 하나\n끝 문장\n\n3. 셋째부터"))
    assert out == '<ul><li>하나</li></ul><p>끝 문장</p><ol start="3"><li>셋째부터</li></ol>'


def test_empty_body() -> None:
    assert str(report_html("")) == "" and str(report_html(None)) == ""
    assert str(report_html("\n\n  \n")) == ""


def test_real_weekly_report_has_no_raw_markdown(env, app_config, clock) -> None:  # noqa: F811
    """주간 리포트 잡이 실제로 쓰는 형식을 그대로 넣어도 기호가 남지 않는다."""
    ctx = SimpleNamespace(
        tz="Asia/Seoul", clock=clock, repo=env["repo"], config=app_config, reports_dir=None
    )
    text, _ = build_report(ctx)
    out = str(report_html(text))
    assert out.startswith("<h2>주간 리포트 ") and "<h3>다음 주 추천 주제</h3>" in out
    assert "**" not in out and "\n" not in out


def test_reports_page_renders_document_not_pre(env, clock) -> None:  # noqa: F811
    d = env["state"].reports_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "weekly-2026-11-09.md").write_text(SAMPLE, encoding="utf-8")
    old = (clock.now() - timedelta(days=7)).date()
    (d / f"weekly-{old}.md").write_text("# 지난 리포트\n\n- **굵게** 한 줄\n", encoding="utf-8")
    html = env["client"].get("/reports").text
    assert '<article class="panel report">' in html and "<pre" not in html
    assert "<h3>요약</h3>" in html and "## 요약" not in html
    old_html = env["client"].get(f"/reports?name=weekly-{old}.md").text  # 지난 파일도 같은 변환
    assert "<li><strong>굵게</strong> 한 줄</li>" in old_html


def test_empty_report_file_says_so(env) -> None:  # noqa: F811
    d = env["state"].reports_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "weekly-2026-11-09.md").write_text("", encoding="utf-8")
    assert "비어 있습니다" in env["client"].get("/reports").text
