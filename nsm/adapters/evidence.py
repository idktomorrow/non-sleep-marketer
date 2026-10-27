"""실패 증거 저장(배치 E1, ADR 0054).

어댑터가 실패를 던지는 순간 `<NSM_HOME>/logs/shots/<플랫폼>-<YYYYMMDD-HHMMSS>/`에 남긴다.
- screenshot.png: 실패한 탭의 화면(Aside 스크립트가 탭을 닫기 전에 찍어 보낸다)
- dialogs.json: role=dialog 개수, 보이는 글(앞부분), svg title·aria-label 목록, 현재 주소
- log-tail.txt: 프로그램 로그의 마지막 50줄

키·토큰·쿠키는 남기지 않는다: 쿠키는 처음부터 수집하지 않고, 글·주소·로그는 비밀처럼 보이는 값을 지운다.
저장이 실패해도 원래 오류를 덮지 않는다(호출부가 None을 받고 그대로 원래 예외를 던진다).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nsm.config import paths

log = logging.getLogger(__name__)

LOG_TAIL_LINES = 50
MAX_TEXT = 4000
MAX_LABELS = 200
_STAMP = "%Y%m%d-%H%M%S"
_NAME = re.compile(r"^(?P<platform>[a-z_]+)-(?P<stamp>\d{8}-\d{6})(?:-\d+)?$")

# 이름=값 꼴의 비밀(쿠키·토큰·세션·키)과 API 키처럼 생긴 값
_SECRET_PAIR = re.compile(
    r"(?i)(cookie|set-cookie|authorization|bearer|token|access_token|sessionid|session|csrftoken|"
    r"ds_user_id|api[_-]?key|password|passwd|secret)(\S*?\s*[:=]\s*)(?:bearer\s+)?[^\s&;,\"']+"
)
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,})"
)
_SECRET_QUERY = re.compile(r"(?i)token|key|session|auth|sig|secret|password|code")


def redact(text: str) -> str:
    """비밀처럼 보이는 값을 지운다(증거 글·로그용)."""
    text = _SECRET_PAIR.sub(r"\1\2<지움>", text or "")
    return _SECRET_VALUE.sub("<지움>", text)


def redact_url(url: str) -> str:
    """주소의 쿼리 중 토큰·키처럼 보이는 값을 지운다."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return redact(url or "")
    q = [(k, "<지움>" if _SECRET_QUERY.search(k) else v) for k, v in parse_qsl(parts.query, True)]
    return redact(urlunsplit(parts._replace(query=urlencode(q))))


def shots_dir() -> Path:
    return paths.logs_dir() / "shots"


def _log_tail(log_file: Path, n: int = LOG_TAIL_LINES) -> str:
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(로그 파일을 읽지 못함)"
    return "\n".join(redact(line) for line in lines[-n:]) + "\n"


def _new_folder(root: Path, platform: str, now: datetime) -> Path:
    base = f"{platform}-{now.strftime(_STAMP)}"
    d = root / base
    i = 1
    while d.exists():  # 같은 초에 두 번 실패해도 덮어쓰지 않게
        d = root / f"{base}-{i}"
        i += 1
    d.mkdir(parents=True)
    return d


def save_evidence(
    platform: str,
    now: datetime,
    evidence: dict[str, Any] | None = None,
    *,
    error: str = "",
    kind: str = "",
    root: Path | None = None,
    log_file: Path | None = None,
) -> Path | None:
    """증거 폴더를 만들고 경로를 돌려준다. 어떤 이유로든 실패하면 None(원래 오류를 덮지 않는다)."""
    try:
        d = _new_folder(root or shots_dir(), platform, now.astimezone())
        ev = evidence or {}
        shot = ev.get("screenshot")
        if isinstance(shot, str) and shot:
            (d / "screenshot.png").write_bytes(base64.b64decode(shot, validate=False))
        info = {
            "platform": platform,
            "kind": kind,
            "error": redact(error)[:1000],
            "url": redact_url(str(ev.get("url", ""))),
            "dialogs": int(ev.get("dialogs", 0) or 0),
            "text": redact(str(ev.get("text", "")))[:MAX_TEXT],
            "labels": [redact(str(x))[:200] for x in (ev.get("labels") or [])][:MAX_LABELS],
            "captured": bool(ev),
        }
        (d / "dialogs.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (d / "log-tail.txt").write_text(
            _log_tail(log_file or paths.logs_dir() / "nsm.log"), encoding="utf-8"
        )
        return d
    except Exception as e:  # noqa: BLE001  증거 저장 실패가 원래 오류를 가리면 안 된다
        log.warning("실패 증거 저장 실패(원래 오류는 그대로): %s", e)
        return None


def folder_time(d: Path) -> datetime | None:
    """폴더 이름의 시각(현지). 이름이 다르면 None."""
    m = _NAME.match(d.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("stamp"), _STAMP).astimezone()
    except ValueError:
        return None


def prune_evidence(now: datetime, keep_days: int, root: Path | None = None) -> int:
    """keep_days일 넘은 증거 폴더를 지운다. 이름 형식이 다른 폴더는 건드리지 않는다."""
    base = root or shots_dir()
    if not base.is_dir():
        return 0
    cutoff = now.astimezone() - timedelta(days=keep_days)
    removed = 0
    for d in base.iterdir():
        t = folder_time(d) if d.is_dir() else None
        if t is not None and t < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
