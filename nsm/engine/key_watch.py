"""API 키 만료일 메모·미리 알림, 하루 한 번 자동 키 확인, 실행 중 키 오류 알림(C53, ADR 0077).

프로그램은 키 만료일을 API로 알 수 없다(Anthropic은 키 만료 조회가 없고 Gemini 키는 만료가 없다). 그래서
① 사람이 적은 만료일 메모로 미리 알리고 ② 설정 화면 "확인"과 같은 모델 목록 조회(비용 0)를 24시간에 한 번
health 잡 끝에서 돌려 실제로 감지하고 ③ 생성·분류 중 401/403을 만나면 바로 알린다.

설정 저장소(키 값이 아니라 날짜·상태라 keyring 아님):
- `key_expiry` = {키 이름: "YYYY-MM-DD"}
- `key_expiry_notified` = {키 이름: {"date": 메모한 만료일, "stage": 마지막 단계, "on": 보낸 날}}
- `key_check` = {키 이름: {"at": 확인 시각, "code": 결과, "alerted": {"code", "on"}(오류 알림을 보냈을 때만)}}
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from nsm.config.secrets import KEY_NAMES, KEYS
from nsm.domain.policy import local

log = logging.getLogger(__name__)

EXPIRY_KEY = "key_expiry"
NOTIFIED_KEY = "key_expiry_notified"
CHECK_KEY = "key_check"
CHECK_EVERY = timedelta(hours=24)
STAGES = (7, 3, 1, 0)  # 남은 날이 이 값이 되는 날 한 번씩. 지난 뒤(-1)는 하루 한 번
EXPIRED = -1
WARN_WITHIN = 7  # 화면 배지·첫 화면 배너를 띄우는 남은 날
FIX = "설정 → API 키에서 새 키를 저장하세요(재시작 불필요)"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CHECK_TEXT = {  # nsm/app/keys.py CHECK_TEXT와 같은 말(엔진이 app을 import하지 않게 여기 둠)
    "ok": "연결됨",
    "invalid": "키가 틀렸거나 만료됨",
    "limit": "키는 맞지만 한도·결제 확인 필요",
    "network": "연결 실패",
    "unknown": "확인하지 못함",
}


def label(name: str) -> str:
    return KEY_NAMES.get(name, name)


def today(now: datetime, tz: str) -> date:
    return local(now, tz).date()


def parse_date(text: str) -> date | None:
    """`<input type="date">` 값(YYYY-MM-DD). 형식이 틀리면 None."""
    text = text.strip()
    if not _DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _get(repo, key: str) -> dict[str, Any]:
    v = repo.get_setting(key, {})
    return dict(v) if isinstance(v, dict) else {}


def expiry_dates(repo) -> dict[str, date]:
    out = {}
    for name, v in _get(repo, EXPIRY_KEY).items():
        d = parse_date(str(v))
        if d is not None:
            out[name] = d
    return out


def set_expiry(repo, name: str, d: date | None) -> None:
    """만료일 메모를 바꾼다(None이면 지움). 알림 단계는 날짜가 바뀌면 notify_expiry가 처음부터 센다."""
    memo = _get(repo, EXPIRY_KEY)
    if d is None:
        memo.pop(name, None)
    else:
        memo[name] = d.isoformat()
    repo.set_setting(EXPIRY_KEY, memo)
    if d is None:
        _pop(repo, NOTIFIED_KEY, name)


def _pop(repo, key: str, name: str) -> None:
    v = _get(repo, key)
    if name in v:
        v.pop(name)
        repo.set_setting(key, v)


def forget_key(repo, name: str) -> None:
    """키를 지웠을 때: 만료일 메모·알림 상태·자동 확인 상태를 모두 지운다."""
    for key in (EXPIRY_KEY, NOTIFIED_KEY, CHECK_KEY):
        _pop(repo, key, name)


def clear_check(repo, name: str) -> None:
    """새 키를 저장했는데 확인 결과를 믿을 수 없을 때(연결 실패 등): 옛 키의 결과를 지워 다음 점검에서 바로 확인."""
    _pop(repo, CHECK_KEY, name)


def record_check(repo, name: str, code: str, at: datetime) -> dict[str, Any]:
    """확인 결과를 `key_check`에 적는다. "ok"면 옛 오류 알림 상태를 지운다. 적은 항목을 돌려준다."""
    checks = _get(repo, CHECK_KEY)
    prev = checks.get(name) if isinstance(checks.get(name), dict) else {}
    cur: dict[str, Any] = {"at": at.isoformat(), "code": code}
    alerted = prev.get("alerted")
    if code != "ok" and isinstance(alerted, dict):
        cur["alerted"] = alerted
    checks[name] = cur
    repo.set_setting(CHECK_KEY, checks)
    return cur


def _alert_due(entry: dict[str, Any], code: str, day: date) -> bool:
    """invalid는 하루 한 번, limit은 같은 상태가 이어지는 동안 한 번."""
    a = entry.get("alerted") if isinstance(entry.get("alerted"), dict) else {}
    if code == "invalid":
        return not (a.get("code") == "invalid" and a.get("on") == day.isoformat())
    if code == "limit":
        return a.get("code") != "limit"
    return False


def _mark_alerted(repo, name: str, code: str, day: date) -> None:
    checks = _get(repo, CHECK_KEY)
    entry = checks.get(name) if isinstance(checks.get(name), dict) else {}
    entry["alerted"] = {"code": code, "on": day.isoformat()}
    checks[name] = entry
    repo.set_setting(CHECK_KEY, checks)


def key_error(ctx, name: str, title: str | None = None) -> bool:
    """실행 중 401/403을 만났을 때: `key_check`를 invalid로 적고 알림은 하루 한 번. 보냈으면 True.

    같은 날 health의 자동 확인은 24시간이 안 지나 다시 부르지 않고, 불러도 하루 한 번 규칙으로 겹치지 않는다."""
    now = ctx.clock.now()
    day = today(now, ctx.tz)
    entry = record_check(ctx.repo, name, "invalid", now)
    if not _alert_due(entry, "invalid", day):
        return False
    _mark_alerted(ctx.repo, name, "invalid", day)
    ctx.notifier.notify(
        title or f"{label(name)} API 키 오류",
        f"{label(name)} 키가 틀렸거나 만료돼 API가 거절했습니다(401/403). {FIX}",
        "error",
    )
    return True


def looks_like_key_error(e: BaseException) -> bool:
    """API가 키를 거절한 예외인가: HTTP 401/403(anthropic 예외의 status_code), Gemini의 400 API_KEY_INVALID."""
    if getattr(e, "status_code", None) in (401, 403):
        return True
    text = str(e)
    return "API_KEY_INVALID" in text or bool(re.search(r"HTTP 40[13]\b", text))


# ------------------------------------------------------------------ 만료일 알림


def stage_of(days_left: int) -> int | None:
    """남은 날 → 알림 단계. 8일 이상이면 None. 꺼져 있다 켜져 그날을 놓쳐도 다음 단계 전까지는 그 단계로 본다."""
    if days_left < 0:
        return EXPIRED
    for s in reversed(STAGES):  # 0, 1, 3, 7
        if days_left <= s:
            return s
    return None


def expiry_title(name: str, days_left: int) -> str:
    if days_left < 0:
        return f"{label(name)} 키가 만료됐습니다"
    if days_left == 0:
        return f"{label(name)} 키가 오늘 만료됩니다"
    return f"{label(name)} 키 만료 {days_left}일 전"


def notify_expiry(ctx, names: list[str]) -> list[str]:
    """메모한 만료일로 미리 알린다. 보낸 알림 제목을 돌려준다."""
    day = today(ctx.clock.now(), ctx.tz)
    memo = expiry_dates(ctx.repo)
    sent_state = _get(ctx.repo, NOTIFIED_KEY)
    before = dict(sent_state)
    sent: list[str] = []
    for name in names:
        d = memo.get(name)
        if d is None:
            continue
        left = (d - day).days
        stage = stage_of(left)
        prev = sent_state.get(name) if isinstance(sent_state.get(name), dict) else {}
        if prev.get("date") != d.isoformat():
            prev = {}  # 메모 날짜가 바뀌면 처음부터
        if stage is None:
            continue
        last = prev.get("stage")
        if stage == EXPIRED:
            due = last != EXPIRED or prev.get("on") != day.isoformat()
        else:
            due = not isinstance(last, int) or stage < last
        if not due:
            continue
        title = expiry_title(name, left)
        when = d.strftime("%m-%d")
        body = (
            f"{label(name)} 키 만료일({when})이 지났습니다. {FIX}"
            if left < 0
            else f"{label(name)} 키가 {when}에 만료됩니다({'오늘' if left == 0 else f'{left}일 남음'}). {FIX}"
        )
        ctx.notifier.notify(title, body, "warn" if left > 0 else "error")
        sent_state[name] = {"date": d.isoformat(), "stage": stage, "on": day.isoformat()}
        sent.append(title)
    if sent_state != before:
        ctx.repo.set_setting(NOTIFIED_KEY, sent_state)
    return sent


# ------------------------------------------------------------------ 자동 확인


def auto_check(ctx, present: dict[str, str], check: Callable) -> list[str]:
    """등록된 키마다 24시간에 한 번 모델 목록 조회(비용 0). 보낸 알림 제목을 돌려준다."""
    from nsm.engine.context import is_interrupted

    now = ctx.clock.now()
    day = today(now, ctx.tz)
    sent: list[str] = []
    for name, value in present.items():
        if is_interrupted(ctx):  # 네트워크를 타니 끄는 중이면 그만
            break
        entry = _get(ctx.repo, CHECK_KEY).get(name)
        at = _parse_at(entry.get("at")) if isinstance(entry, dict) else None
        if at is not None and now - at < CHECK_EVERY:
            continue
        try:
            code, status = check(name, value)
        except Exception as e:  # noqa: BLE001  확인 실패가 health를 막지 않게
            log.warning("키 자동 확인 실패(%s): %s", name, type(e).__name__)
            code, status = "unknown", None
        log.info("키 자동 확인(%s): %s%s", name, code, f"(HTTP {status})" if status else "")
        entry = record_check(ctx.repo, name, code, now)
        if not _alert_due(entry, code, day):
            continue
        _mark_alerted(ctx.repo, name, code, day)
        if code == "invalid":
            title = f"{label(name)} 키가 틀렸거나 만료됨"
            ctx.notifier.notify(title, f"하루 한 번 자동 확인에서 거절됐습니다. {FIX}", "error")
        else:
            title = f"{label(name)} 키 한도·결제 확인 필요"
            ctx.notifier.notify(
                title,
                f"키는 맞지만 한도나 결제 때문에 쓸 수 없을 수 있습니다(HTTP {status}). "
                "공급자 콘솔에서 요금제·결제를 확인하세요",
                "warn",
            )
        sent.append(title)
    return sent


def _parse_at(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def present_keys(secrets) -> dict[str, str]:
    out = {}
    for name in KEYS:
        try:
            v = secrets.get(name)
        except Exception:  # noqa: BLE001  keyring 백엔드 오류
            v = None
        if v:
            out[name] = v
    return out


def check_keys(ctx) -> list[str]:
    """health 잡 끝 단계(C53): 만료일 미리 알림 + 24시간에 한 번 자동 확인. 보낸 알림 제목들."""
    if ctx.secrets is None:
        return []
    present = present_keys(ctx.secrets)
    sent = notify_expiry(ctx, list(present))
    if ctx.check_key is not None:
        sent += auto_check(ctx, present, ctx.check_key)
    return sent


# ------------------------------------------------------------------ 화면


def key_view(repo, now: datetime, tz: str) -> dict[str, dict[str, Any]]:
    """설정 화면 상태 칸: 키마다 만료 문구·배지, 마지막 자동 확인 문구."""
    day = today(now, tz)
    memo = expiry_dates(repo)
    checks = _get(repo, CHECK_KEY)
    out: dict[str, dict[str, Any]] = {}
    for name in KEYS:
        v: dict[str, Any] = {}
        if (d := memo.get(name)) is not None:
            left = (d - day).days
            v["expires"] = d.isoformat()
            if left < 0:
                v["expiry_text"], v["expiry_badge"] = f"만료됨({d:%m-%d})", "bad"
            else:
                rest = "오늘" if left == 0 else f"{left}일 남음"
                v["expiry_text"] = f"만료 {d:%m-%d} ({rest})"
                v["expiry_badge"] = "warn" if left <= WARN_WITHIN else ""
        c = checks.get(name)
        if isinstance(c, dict) and (at := _parse_at(c.get("at"))) is not None:
            text = CHECK_TEXT.get(str(c.get("code")), "확인하지 못함")
            v["auto_text"] = f"자동 확인 {local(at, tz):%m-%d %H:%M} {text}"
            v["auto_bad"] = c.get("code") == "invalid"
        out[name] = v
    return out


def key_banner(repo, now: datetime, tz: str) -> dict[str, Any] | None:
    """첫 화면 맨 위 한 줄: 만료 7일 이하·만료됨·자동 확인 invalid인 키. 없으면 None."""
    day = today(now, tz)
    memo = expiry_dates(repo)
    checks = _get(repo, CHECK_KEY)
    lines: list[str] = []
    bad = False
    for name in KEYS:
        c = checks.get(name)
        if isinstance(c, dict) and c.get("code") == "invalid":
            lines.append(f"{label(name)} 키가 틀렸거나 만료됐습니다")
            bad = True
            continue
        d = memo.get(name)
        if d is None:
            continue
        left = (d - day).days
        if left < 0:
            lines.append(f"{label(name)} 키가 만료됐습니다({d:%m-%d})")
            bad = True
        elif left == 0:
            lines.append(f"{label(name)} 키가 오늘 만료됩니다")
            bad = True
        elif left <= WARN_WITHIN:
            lines.append(f"{label(name)} 키가 {left}일 뒤 만료됩니다")
    if not lines:
        return None
    return {"lines": lines, "level": "bad" if bad else "warn"}
