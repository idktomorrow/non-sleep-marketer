"""로그인 안 됨의 세부 상태(E5, ADR 0060).

health와 댓글 확인·게시 전 확인(nsmAssertHandle)이 "로그인돼 있지 않음"을 볼 때, 그 순간 탭의 주소·보이는 글·
라벨(nsmEvidence)로 무엇이 막고 있는지 나눈다. 사람이 해야 할 일이 상태마다 다르다.

- logged_out: 그냥 로그아웃(로그인 폼)
- challenge: 본인 확인·captcha("사람임을 확인하세요")
- verify: 문자·이메일 코드 요구("휴대폰 번호를 입력하세요 / 코드 보내기")
- two_factor: 두 단계 인증
- restricted: 계정 제한·정지 안내
- unknown: 로그인은 안 됐는데 화면으로 원인을 모름

패턴은 `data/auth_states.json` 한 곳에 있다. 화면 문구를 주소보다 먼저 본다: 인스타그램은 본인 확인과 문자 인증도
/accounts/suspended/ 주소로 띄운다(2026-10-29 증거).

빈 화면: 로그인·확인 주소가 아닌데 보이는 글과 라벨이 모두 비었으면 로그인 풀림이 아니라 페이지가 덜 열린 것이다
(2026-11-02 스레드). 로그인 폼이 떠 있으면 글이 있으므로 진짜 로그아웃은 여기 걸리지 않는다.

로그인된 화면(C39, ADR 0064): 핸들을 못 읽었어도 로그인한 사람만 보는 문구(`logged_in_markers`, 예: 스레드
"…님에게 답글 남기기")가 보이고 위 규칙 어디에도 안 걸리면 로그아웃이 아니라 화면 변경이다(2026-11-05 스레드,
incidents/0012). 같은 규칙을 브라우저 쪽 `nsmAssertHandle`에도 `js_auth_args`로 넘긴다(문구는 JSON 한 곳).
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Any, Literal, get_args

AuthState = Literal["logged_out", "challenge", "verify", "two_factor", "restricted", "unknown"]
AUTH_STATES: tuple[AuthState, ...] = get_args(AuthState)
# 점검이 예외 없이 끝나도 증거를 남기는 상태: 사람이 화면을 봐야 하는 보안 확인과 제한
EVIDENCE_STATES = frozenset({"challenge", "verify", "two_factor", "restricted"})


@cache
def _rules() -> dict[str, Any]:
    raw = json.loads(
        resources.files("nsm.adapters.platforms.data")
        .joinpath("auth_states.json")
        .read_text("utf-8")
    )
    comp = lambda p: re.compile(p, re.IGNORECASE)  # noqa: E731
    return {
        "url_first": [(r["state"], comp(r["pattern"])) for r in raw["url_first"]],
        "text": [(r["state"], comp(r["pattern"])) for r in raw["text_rules"]],
        "url": [(r["state"], comp(r["pattern"])) for r in raw["url_rules"]],
        "check_urls": comp(raw["check_urls"]),
        "login_form": comp(raw["login_form"]),
        "markers": {k: comp(v) for k, v in raw["logged_in_markers"].items()},
        "raw": raw,
    }


def _parts(page: dict[str, Any] | None) -> tuple[str, str, list[str]]:
    page = page or {}
    labels = [str(x) for x in (page.get("labels") or []) if str(x).strip()]
    return str(page.get("url") or ""), str(page.get("text") or "").strip(), labels


def is_blank(page: Any) -> bool:
    """덜 열린 하얀 화면: 주소가 있고, 로그인·확인 주소가 아니고, 글과 라벨이 모두 비었다(셋 다일 때만).

    화면 글을 읽었을 때만 판단한다(`text` 키가 있을 때). 읽기가 시간 초과·실패하면 글이 없는 게 아니라 모르는 것이다.
    """
    if not isinstance(page, dict) or "text" not in page:
        return False
    url, text, labels = _parts(page)
    return bool(url) and not _rules()["check_urls"].search(url) and not text and not labels


def classify(page: Any) -> AuthState:
    """화면 정보로 세부 상태. 두 단계 인증 주소 → 글 → 주소 → 로그인 폼 순서. 화면 정보가 없으면 unknown."""
    if not isinstance(page, dict) or not page:
        return "unknown"
    url, text, labels = _parts(page)
    seen = "\n".join([text, *labels])
    r = _rules()
    for state, pat in r["url_first"]:
        if pat.search(url):
            return state
    for state, pat in r["text"]:
        if pat.search(seen):
            return state
    for state, pat in r["url"]:
        if pat.search(url):
            return state
    if r["login_form"].search(seen):
        return "logged_out"
    return "unknown"


def looks_logged_in(page: Any, platform: str) -> bool:
    """핸들은 못 읽었지만 로그인된 화면인가(C39). 빈 화면이 아니고, 로그인 폼·보안 확인·제한·확인 주소 규칙에
    하나도 안 걸리고(classify가 unknown), 그 플랫폼의 로그인 표지가 화면 글이나 라벨에 있을 때만 참."""
    if not isinstance(page, dict) or not page or is_blank(page):
        return False
    r = _rules()
    pat = r["markers"].get(platform)
    url, text, labels = _parts(page)
    # 로그인·확인 주소(예: /accounts/suspended/) 위의 화면은 표지가 있어도 아니다(JS check_urls와 같은 기준)
    if pat is None or classify(page) != "unknown" or r["check_urls"].search(url):
        return False
    return bool(pat.search("\n".join([text, *labels])))


def js_auth_args(platform: str) -> dict[str, str]:
    """브라우저 쪽 `nsmAssertHandle`이 쓰는 같은 규칙(정규식 원문). 표지가 없는 플랫폼이면 빈 dict.

    blockers는 로그인 폼과 화면 글 규칙(본인 확인·인증·제한)을 합친 것, check_urls는 로그인·확인 주소다.
    둘 중 하나라도 맞으면 표지가 있어도 로그인 문제로 본다(`classify`와 같은 기준).
    """
    raw = _rules()["raw"]
    markers = raw["logged_in_markers"].get(platform)
    if not markers:
        return {}
    blockers = [r["pattern"] for r in raw["text_rules"]] + [raw["login_form"]]
    url_rules = [r["pattern"] for r in raw["url_first"] + raw["url_rules"]] + [raw["check_urls"]]
    return {
        "markers": markers,
        "blockers": "|".join(f"(?:{b})" for b in blockers),
        "check_urls": "|".join(f"(?:{u})" for u in url_rules),
    }
