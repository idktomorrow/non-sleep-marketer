"""R1: 마크다운 조각 규칙(제목 경로, 표·코드 안 자름, 겹침, 크기)."""

from __future__ import annotations

import pytest

from nsm.rag.chunker import chunk_markdown

MD = """# 사용설명서

첫 절 소개 문단입니다.

## 7. 문제 해결

### 장애 화면

장애 화면은 고급 모드에서 봅니다.
"""


def test_heading_path_is_stored_and_prefixed() -> None:
    chunks = chunk_markdown("user-guide.md", MD, size=500, overlap=80)
    paths = [c.heading_path for c in chunks]
    assert paths == [
        "user-guide.md > 사용설명서",
        "user-guide.md > 사용설명서 > 7. 문제 해결 > 장애 화면",
    ]  # 본문 없는 "7. 문제 해결" 절은 조각이 없다
    assert chunks[1].text.splitlines()[0] == "user-guide.md > 사용설명서 > 7. 문제 해결 > 장애 화면"
    assert [c.seq for c in chunks] == [0, 1]


def test_sibling_heading_replaces_previous_level() -> None:
    md = "# A\n\n## B\n\nb 본문\n\n## C\n\nc 본문\n\n# D\n\nd 본문\n"
    assert [c.heading_path for c in chunk_markdown("f.md", md)] == [
        "f.md > A > B",
        "f.md > A > C",
        "f.md > D",
    ]


def test_code_block_is_never_split_even_when_large() -> None:
    code = "```python\n" + "\n".join(f"x{i} = {i}  # 긴 줄" for i in range(80)) + "\n```"
    md = f"# 코드\n\n앞 문단.\n\n{code}\n\n뒤 문단.\n"
    chunks = chunk_markdown("c.md", md, size=200, overlap=40)
    holding = [c for c in chunks if "```python" in c.body]
    assert len(holding) == 1 and holding[0].body.count("```") == 2  # 펜스가 한 조각 안에 둘 다
    assert "# 긴 줄" not in "".join(c.body for c in chunks if c is not holding[0])


def test_heading_like_line_inside_code_is_not_a_heading() -> None:
    md = "# 진짜\n\n```bash\n# 주석이지 제목이 아님\nuv run nsm index\n```\n"
    chunks = chunk_markdown("c.md", md)
    assert [c.heading_path for c in chunks] == ["c.md > 진짜"]


def test_small_table_stays_whole_and_large_table_splits_on_rows_with_header() -> None:
    rows = "\n".join(f"| 항목{i} | 설명 {i} 문장이 조금 깁니다 |" for i in range(40))
    md = f"# 표\n\n| 이름 | 뜻 |\n|---|---|\n{rows}\n"
    chunks = chunk_markdown("t.md", md, size=300, overlap=40)
    assert len(chunks) > 1
    for c in chunks:
        lines = c.body.splitlines()
        assert lines[0] == "| 이름 | 뜻 |" and lines[1] == "|---|---|"  # 조각마다 머리글
        assert all(
            line.startswith("|") and line.endswith("|") for line in lines
        )  # 행이 반쪽 나지 않음
    body_rows = [line for c in chunks for line in c.body.splitlines()[2:]]
    assert body_rows == rows.splitlines()  # 행이 빠지거나 겹치지 않음(표에는 겹침을 넣지 않는다)
    small = chunk_markdown("t.md", "# 표\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", size=300)
    assert len(small) == 1 and "| 1 | 2 |" in small[0].body


def test_overlap_and_size() -> None:
    para = " ".join(f"문장{i}입니다." for i in range(200))
    chunks = chunk_markdown("o.md", f"# 긴 절\n\n{para}\n", size=300, overlap=60)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c.body) <= 300 + 60 + 2  # 목표 + 겹침
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        head = nxt.body[:30]
        assert head in prev.body  # 다음 조각 앞부분이 앞 조각 끝에서 왔다


def test_no_overlap_when_disabled_and_bad_settings_rejected() -> None:
    para = " ".join(f"문장{i}입니다." for i in range(200))
    chunks = chunk_markdown("o.md", f"# 절\n\n{para}\n", size=300, overlap=0)
    joined = " ".join(c.body for c in chunks)
    assert joined.count("문장100입니다.") == 1
    with pytest.raises(ValueError):
        chunk_markdown("o.md", "x", size=100, overlap=100)
