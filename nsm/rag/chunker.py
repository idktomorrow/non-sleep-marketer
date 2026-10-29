"""마크다운을 제목 구조대로 조각낸다(R1, ADR 0056).

규칙:
- 제목(`#`~`######`)마다 절을 나눈다. 조각은 절을 넘지 않는다.
- 조각마다 "파일명 > 상위 제목 > 소제목" 경로를 저장하고 본문 앞 첫 줄에 붙인다(검색 결과만 봐도 어디인지 알게).
- 절 안에서는 문단·목록·표·코드 블록 단위로 목표 크기(`size`)까지 담는다. 표와 코드 블록은 중간에서 자르지 않는다.
  코드 블록은 목표보다 커도 통째로 한 조각이다. 표가 목표보다 크면 **행 경계에서만** 나누고 조각마다 머리글(첫 행과
  구분선)을 다시 붙인다. 행 하나는 쪼개지 않으므로 조각마다 온전한 표가 된다(9천 자짜리 구현 상태 표가 질문 하나에
  통째로 걸리지 않게, ADR 0056).
- 글 문단 하나가 목표보다 크면 문장 경계(없으면 공백)에서 자른다.
- 조각은 목표 크기를 넘지 않게 담되, 겹침을 붙인 조각은 최대 `size + overlap`자까지 될 수 있다.
- 이어지는 조각은 앞 조각 끝의 `overlap`자를 앞에 다시 담는다(문장이 조각 경계에 걸려도 맥락이 남게). 앞 조각이 표·코드로
  끝났으면 겹치지 않는다(표·코드를 반쪽으로 가져오지 않게).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Chunk:
    heading_path: str  # "user-guide.md > 7. 문제 해결 > 장애 화면"
    seq: int  # 파일 안에서의 순번(0부터)
    body: str  # 경로를 붙이기 전 본문
    section: int  # 몇 번째 절에서 나왔나(통계: 절이 여러 조각으로 쪼개진 비율)

    @property
    def text(self) -> str:
        """저장·임베딩할 글: 첫 줄에 제목 경로, 다음 줄부터 본문."""
        return f"{self.heading_path}\n{self.body}"


@dataclass
class _Block:
    text: str
    atomic: bool  # 표·코드: 자르지 않는다


@dataclass
class _Section:
    path: list[str]
    blocks: list[_Block] = field(default_factory=list)


def _sections(name: str, md: str) -> list[_Section]:
    sections = [_Section([name])]
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    kind = ""  # "", "code", "table"

    def flush() -> None:
        nonlocal buf, kind
        text = "\n".join(buf).strip("\n")
        if text.strip():
            sections[-1].blocks.append(_Block(text, atomic=kind in ("code", "table")))
        buf, kind = [], ""

    for line in md.splitlines():
        if kind == "code":
            buf.append(line)
            if FENCE.match(line):
                flush()
            continue
        if FENCE.match(line):
            flush()
            kind = "code"
            buf.append(line)
            continue
        m = HEADING.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
            sections.append(_Section([name, *(t for _, t in stack)]))
            continue
        is_table = line.lstrip().startswith("|")
        if not line.strip():
            flush()
            continue
        if is_table != (kind == "table") and buf:
            flush()
        kind = "table" if is_table else kind if kind else "text"
        buf.append(line)
    flush()
    return [s for s in sections if s.blocks]


def _split_table(text: str, size: int) -> list[str]:
    """큰 표를 행 경계에서 나눈다. 조각마다 머리글(첫 행 + 구분선)을 다시 붙인다."""
    rows = text.split("\n")
    head_n = 2 if len(rows) > 1 and set(rows[1].replace("|", "").strip()) <= set("-: ") else 1
    head, body = rows[:head_n], rows[head_n:]
    base = sum(len(h) + 1 for h in head)
    out: list[str] = []
    cur: list[str] = []
    cur_len = base
    for row in body:
        if cur and cur_len + len(row) + 1 > size:
            out.append("\n".join(head + cur))
            cur, cur_len = [], base
        cur.append(row)
        cur_len += len(row) + 1
    if cur or not out:
        out.append("\n".join(head + cur))
    return out


def _split_sentences(line: str, size: int) -> list[str]:
    """한 줄이 목표보다 길 때: 문장 경계에서, 문장도 길면 공백에서."""
    pieces = [p for p in re.split(r"(?<=[.!?])\s+|(?<=다\.)\s*|(?<=요\.)\s*", line) if p.strip()]
    out: list[str] = []
    cur = ""
    for p in pieces:
        while len(p) > size:
            cut = p.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        joined = f"{cur} {p}".strip() if cur else p
        if len(joined) > size and cur:
            out.append(cur)
            cur = p
        else:
            cur = joined
    if cur:
        out.append(cur)
    return out


def _split_long(text: str, size: int) -> list[str]:
    """글 블록이 너무 길 때: 줄 단위로 담아 줄바꿈(목록 항목)을 지키고, 한 줄이 길면 문장에서 자른다."""
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        parts = [line] if len(line) <= size else _split_sentences(line, size)
        for part in parts:
            add = len(part) + (1 if cur else 0)
            if cur and cur_len + add > size:
                out.append("\n".join(cur))
                cur, cur_len = [], 0
                add = len(part)
            cur.append(part)
            cur_len += add
    if cur:
        out.append("\n".join(cur))
    return out


def _tail(text: str, n: int) -> str:
    """앞 조각 끝 n자 정도를 낱말 경계에서."""
    if n <= 0 or len(text) <= n:
        return text if n > 0 else ""
    t = text[-n:]
    sp = t.find(" ")
    return t[sp + 1 :] if 0 <= sp < n // 2 else t


def _units(blocks: list[_Block], size: int) -> list[_Block]:
    units: list[_Block] = []
    for b in blocks:
        is_code = b.atomic and b.text.lstrip().startswith(("```", "~~~"))
        if len(b.text) <= size or is_code:
            units.append(b)  # 코드 블록은 커도 통째로
        elif b.atomic:  # 큰 표: 행 경계에서, 머리글을 붙여
            units.extend(_Block(t, True) for t in _split_table(b.text, size))
        else:
            units.extend(_Block(p, False) for p in _split_long(b.text, size))
    return units


def _pack(units: list[_Block], size: int, overlap: int) -> list[str]:
    """단위를 목표 크기까지 담고, 이어지는 조각엔 앞 조각 끝을 겹쳐 담는다(표·코드 경계에선 안 겹침)."""
    bodies: list[str] = []
    cur: list[str] = []
    cur_len = 0
    last_atomic = False
    for u in units:
        add = len(u.text) + (2 if cur else 0)
        if cur and cur_len + add > size:
            bodies.append("\n\n".join(cur))
            prev_text, prev_atomic = cur[-1], last_atomic
            cur, cur_len = [], 0
            if overlap and not prev_atomic and not u.atomic:
                t = _tail(prev_text, overlap)
                if t:
                    cur, cur_len = [t], len(t)
            add = len(u.text) + (2 if cur else 0)
        cur.append(u.text)
        cur_len += add
        last_atomic = u.atomic
    if cur:
        bodies.append("\n\n".join(cur))
    return bodies


def chunk_markdown(name: str, md: str, size: int = 500, overlap: int = 80) -> list[Chunk]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("조각 크기는 양수, 겹침은 0 이상이고 크기보다 작아야 합니다")
    chunks: list[Chunk] = []
    for si, sec in enumerate(_sections(name, md)):
        path = " > ".join(sec.path)
        for body in _pack(_units(sec.blocks, size), size, overlap):
            chunks.append(Chunk(path, len(chunks), body, si))
    return chunks
