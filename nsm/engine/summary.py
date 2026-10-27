"""작업 결과 → 사람이 읽는 한 줄 (배치 C10).

실행 기록·"지금 실행" 알림·로그 파일이 모두 이 문장을 쓴다. 예전에는 `reply: new=0, auto_replied=0, ...`
처럼 내부 값을 그대로 보여 줬다. DB에 남은 예전 형식 줄도 화면에 보일 때 같은 문장으로 바꾼다(`readable`).
"""

from __future__ import annotations

import ast
import re
from typing import Any

PLATFORMS = {"instagram": "인스타그램", "threads": "스레드", "naver_blog": "네이버 블로그"}
TEMPLATES = {"info": "정보형", "list": "리스트형", "compare": "비교형"}
SEP = " · "


def _n(counts: dict[str, Any], key: str) -> int:
    v = counts.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _collect(c: dict[str, Any]) -> list[str]:
    new, dup = _n(c, "new"), _n(c, "duplicate")
    parts = [f"새 자료 {new}개" + (f"(이미 본 자료 {dup}개 뺌)" if dup else "")]
    if "relevant" in c:
        looked = f"{_n(c, 'classified')}개를 살펴 " if _n(c, "classified") else ""
        parts.append(f"{looked}관련 자료 {_n(c, 'relevant')}개")
    if _n(c, "excluded_genre"):
        parts.append(f"후기·일기 {_n(c, 'excluded_genre')}개 제외")
    if _n(c, "excluded_word"):
        parts.append(f"제외어가 든 글 {_n(c, 'excluded_word')}개 뺌")
    if _n(c, "bodies"):
        parts.append(f"원문 {_n(c, 'bodies')}개 읽음")
    return parts


def _generate(c: dict[str, Any]) -> list[str]:
    created = _n(c, "created")
    if not created:
        parts = ["새 원고 없음"]
    else:
        detail = [
            f"{label} {_n(c, k)}개"
            for k, label in (("pending_review", "검토 대기"), ("approved", "바로 승인"))
            if _n(c, k)
        ]
        parts = [f"원고 {created}개 만듦" + (f"({', '.join(detail)})" if detail else "")]
    if templates := c.get("templates"):
        parts.append("형식: " + ", ".join(TEMPLATES.get(t, str(t)) for t in templates))
    if _n(c, "regenerated"):
        parts.append(f"규칙에 걸려 다시 쓴 원고 {_n(c, 'regenerated')}개")
    if c.get("cost_cap_hit"):
        parts.append("오늘 비용 상한에 닿아 멈춤")
    return parts


def _keyword(c: dict[str, Any]) -> list[str]:
    """키워드 원고(C15): 예) "'치이카와 굿즈' 원고 1개 만듦(검토 대기 1개) · 찾은 새 글 8개, 쓸 만한 자료 3개"."""
    kw = c.get("keyword")
    made = _generate(c)
    if kw:
        made[0] = f"'{kw}' {made[0]}"
    found = f"찾은 새 글 {_n(c, 'new')}개" + (
        f"(이미 본 글 {_n(c, 'duplicate')}개)" if _n(c, "duplicate") else ""
    )
    parts = [*made, f"{found}, 쓸 만한 자료 {_n(c, 'relevant')}개"]
    if _n(c, "excluded_word"):
        parts.append(f"제외어가 든 글 {_n(c, 'excluded_word')}개 뺌")
    return parts


def _publish(c: dict[str, Any]) -> list[str]:
    published, failed = _n(c, "published"), _n(c, "failed")
    done = [f"{published}건 올림" if published else "", f"{failed}건 실패" if failed else ""]
    parts = [", ".join(d for d in done if d) or "올린 글 없음"]
    waits = c.get("waits") or []
    if waits:
        # 올리거나 실패한 곳이 있으면 나머지 플랫폼 이야기다
        label = "안 올린 곳: " if published or failed else "이유: "
        parts.append(label + ", ".join(map(str, waits)))
    return parts


def _reply(c: dict[str, Any]) -> list[str]:
    new = _n(c, "new")
    done = [
        f"{label} {_n(c, k)}개"
        for k, label in (
            ("auto_replied", "자동 답글"),
            ("hidden", "숨김"),
            ("escalated", "사람 확인으로 넘김"),
            ("ignored", "답할 필요 없음(스팸·하루 한도)"),
        )
        if _n(c, k)
    ]
    if new:
        parts = [f"새 댓글 {new}개"]
    elif done:  # 지난번에 처리하다 멈춘 댓글을 이번에 처리했다
        parts = ["새 댓글 없음, 지난번 남은 댓글 처리"]
    else:
        parts = ["새 댓글 없음"]
    if done:
        parts.append(", ".join(done))
    return parts


def _health(c: dict[str, Any]) -> list[str]:
    paused = c.get("paused") or []
    if paused:
        names = ", ".join(PLATFORMS.get(p, str(p)) for p in paused)
        return [f"로그인이 필요해 멈춘 곳: {names}"]
    return ["로그인 정상"]


def _metrics(c: dict[str, Any]) -> list[str]:
    return [f"게시물 지표 {_n(c, 'posts')}개, 계정 지표 {_n(c, 'accounts')}개 읽음"]


def _weekly(c: dict[str, Any]) -> list[str]:
    parts = [f"지난 7일 게시물 {_n(c, 'posts')}개 정리"]
    if c.get("file"):
        parts.append(f"파일 {c['file']}")
    return parts


def _maintenance(c: dict[str, Any]) -> list[str]:
    parts = []
    if c.get("backup"):
        parts.append(f"데이터 백업 {c['backup']}({c.get('backup_mb', 0)}MB)")
        if _n(c, "backups_removed"):
            parts.append(f"오래된 백업 {_n(c, 'backups_removed')}개 지움")
    if _n(c, "runs_pruned"):
        parts.append(f"오래된 실행 기록 {_n(c, 'runs_pruned')}줄 정리")
    if _n(c, "sources_blanked"):
        parts.append(f"관련 없는 자료 {_n(c, 'sources_blanked')}건 본문 비움")
    if _n(c, "sources_pruned"):
        parts.append(f"오래된 자료 판정 기록 {_n(c, 'sources_pruned')}줄 정리")
    if _n(c, "evidence_pruned"):
        parts.append(f"오래된 실패 증거 {_n(c, 'evidence_pruned')}개 지움")
    return parts or ["정리할 것 없음"]


_JOBS = {
    "collect": _collect,
    "generate": _generate,
    "keyword": _keyword,
    "publish": _publish,
    "reply": _reply,
    "health": _health,
    "metrics": _metrics,
    "weekly_report": _weekly,
    "maintenance": _maintenance,
}


def _plain(key: str, value: Any) -> str:
    """문장 함수가 없는 잡(새로 더했는데 빠뜨린 경우)의 값. 목록 표기([…])는 풀어 쓴다."""
    if isinstance(value, list | tuple):
        value = ", ".join(map(str, value)) or "없음"
    return f"{key} {value}"


def describe(
    job: str,
    skipped: str = "",
    counts: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> str:
    """예: "새 댓글 없음 · 인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"."""
    if skipped:
        return f"건너뜀: {skipped}"
    c = counts or {}
    fn = _JOBS.get(job)
    parts = (
        fn(c) if fn else [_plain(k, v) for k, v in c.items() if k not in ("rate_limited", "stalls")]
    )
    for key in ("rate_limited", "stalls"):  # 여러 작업이 붙이는 문장 목록
        if extra := c.get(key):
            parts.extend(map(str, extra))
    text = SEP.join(p for p in parts if p) or "바뀐 것 없음"
    if errors:
        text += f"{SEP}오류 {len(errors)}건: {errors[0]}"
    return text


# 예전 형식: "reply: new=0, auto_replied=0, rate_limited=['…'] / 오류 1건: …" 또는 "publish: 건너뜀 (…)"
_LEGACY = re.compile(
    r"^(?P<job>[a-z_]+): (?P<body>.*?)(?: / 오류 (?P<n>\d+)건: (?P<err>.*))?$", re.S
)
_PAIR = re.compile(r"(\w+)=(.*?)(?=, \w+=|$)", re.S)


def readable(job: str, message: str | None) -> str:
    """실행 기록 한 줄을 화면용으로. 예전 형식이면 새 문장으로 바꾸고, 아니면 그대로."""
    if not message:
        return ""
    m = _LEGACY.match(message)
    if not m or m["job"] != job:
        return message
    body = m["body"]
    errors = [m["err"]] * int(m["n"]) if m["err"] else []
    if body.startswith("건너뜀 (") and body.endswith(")"):
        return describe(job, skipped=body[len("건너뜀 (") : -1])
    if body == "변화 없음":
        return describe(job, counts={}, errors=errors)
    counts = _legacy_counts(body)
    return describe(job, counts=counts, errors=errors) if counts else message


def _legacy_counts(body: str) -> dict[str, Any]:
    """ "k=v, k=v" → dict. 값은 예전 summary()가 f"{k}={v}"로 쓴 것이다.

    - 목록은 repr이라 literal_eval로 읽는다. 목록 글자 안에 ", x="가 있으면 잘린 조각을 다음 조각과 이어 다시 읽는다.
    - 따옴표 없는 글자(file=weekly-….md)는 literal_eval이 못 읽으니 글자 그대로 둔다.
    """
    pairs = _PAIR.findall(body)
    counts: dict[str, Any] = {}
    i = 0
    while i < len(pairs):
        key, raw = pairs[i]
        raw = raw.strip()
        j = i
        while raw[:1] in "[('\"{" and j + 1 < len(pairs) and not _parses(raw):
            j += 1
            raw = f"{raw}, {pairs[j][0]}={pairs[j][1].strip()}"
        counts[key] = ast.literal_eval(raw) if _parses(raw) else raw
        i = j + 1
    return counts


def _parses(raw: str) -> bool:
    try:
        ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return False
    return True
