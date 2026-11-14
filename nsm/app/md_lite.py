"""주간 리포트 마크다운 → 화면 HTML (배치 C45, ADR 0072).

주간 리포트(`nsm/engine/weekly_report.py`)가 쓰는 마크다운은 좁다: 제목(#, ##), 굵게(**…**), 목록(-, 1.),
들여쓴 하위 목록, 빈 줄. 이 범위만 바꾸고 나머지 줄은 문단으로 둔다. 외부 라이브러리를 더하지 않는다.

`ask_format.answer_html`과 같은 태도: 줄을 먼저 전부 이스케이프하고, 그 위에 우리가 만든 태그만 넣는다.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_HEAD = re.compile(r"^(#{1,3})\s+(.+)$")
_ITEM = re.compile(r"^(\s*)(?:([-*])|(\d{1,3})\.)\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    """한 줄 글: 이스케이프 → **굵게**."""
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text.strip(), quote=True))


def report_html(text: str | None) -> Markup:
    """리포트 본문을 안전한 HTML로. 목록은 들여쓰기(공백 2칸 이상)로 한 단계 아래에 둔다."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # 열린 목록: (들여쓰기, "ul"·"ol")
    para: list[str] = []

    def close_para() -> None:
        if para:
            out.append(f"<p>{'<br>'.join(para)}</p>")
            para.clear()

    def close_lists(indent: int = -1) -> None:
        """indent보다 깊게 열린 목록을 닫는다(-1이면 전부)."""
        while stack and stack[-1][0] > indent:
            out.append(f"</li></{stack.pop()[1]}>")

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_para()
            continue
        if m := _HEAD.match(line):
            close_para()
            close_lists()
            level = min(len(m.group(1)) + 1, 4)  # # → h2(화면 제목 h1은 페이지가 쓴다)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if m := _ITEM.match(line):
            close_para()
            indent = len(m.group(1).expandtabs(4))
            kind = "ol" if m.group(3) else "ul"
            if stack and indent < stack[-1][0]:
                close_lists(indent)
            if stack and stack[-1][0] == indent and stack[-1][1] != kind:
                out.append(f"</li></{stack.pop()[1]}>")
            if not stack or indent > stack[-1][0]:
                start = f' start="{int(m.group(3))}"' if kind == "ol" and m.group(3) != "1" else ""
                out.append(f"<{kind}{start}><li>")
                stack.append((indent, kind))
            else:
                out.append("</li><li>")
            out.append(_inline(m.group(4)))
            continue
        close_lists()
        para.append(_inline(line))
    close_para()
    close_lists()
    return Markup("".join(out))  # noqa: S704  위에서 줄마다 이스케이프한 뒤 우리가 만든 태그만 넣었다
