"""콘텐츠 품질 규칙 (배치 Q1, 2026-10-12 드라이런에서 발견한 문제).

프롬프트에만 적어 두면 모델이 어겨도 그대로 나간다. 그래서 코드로 검사하거나 고칠 수 있는 규칙은 여기 둔다.
모두 순수 함수라 엔진·어댑터 어디서든 쓰고, 단위 테스트로 고정한다.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from nsm.domain.models import GeneratedContent, Persona

# ------------------------------------------------------------------ 관련성: 제외 장르

# 방문 후기·여행기·개인 일기. 정보성 글이 아니고, 후기형은 공정위 지침 위험으로 쓰지 않는다(ADR 0007).
_EXCLUDED_GENRE = re.compile(
    r"내돈내산|방문\s*후기|방문기|다녀왔|다녀온|여행기|여행\s*(?:후기|일기|중에|브이로그)|"
    r"카페\s*투어|투어\s*후기|브이로그|vlog|일상\s*(?:기록|일기|브이로그)|일기|데이트\s*(?:코스|후기)|"
    r"(?:구매|사용|솔직|체험|시음|맛집|카페)\s*후기|후기\s*[♥❤!]|웨이팅",
    re.IGNORECASE,
)


def excluded_genre(title: str, text: str) -> str | None:
    """제목(과 본문 앞부분)이 후기·여행기·일기 같으면 걸린 말을 돌려준다. 분류기를 부르기 전에 거른다."""
    m = _EXCLUDED_GENRE.search(f"{title}\n{text[:120]}")
    return m.group(0) if m else None


# ------------------------------------------------------------------ 카드 목록 번호

_BULLET_MARK = re.compile(r"^\s*(?:\d{1,2}\s*[.)]|[①-⑳]|[-•·*▪︎✔✅☑]️?)\s*")


def strip_bullet_marker(s: str) -> str:
    """템플릿이 번호·점을 붙이므로 문장 앞의 "1.", "①", "-" 같은 표시는 뗀다(이중 번호 방지)."""
    out = _BULLET_MARK.sub("", s, count=1).strip()
    return out or s.strip()


def normalize_bullets(gen: GeneratedContent) -> GeneratedContent:
    slides = []
    for s in gen.deck.slides:
        if s.kind in ("body", "list") and s.bullets:
            s = s.model_copy(update={"bullets": [strip_bullet_marker(b) for b in s.bullets]})
        elif s.kind == "compare":
            s = s.model_copy(
                update={
                    "left": s.left.model_copy(
                        update={"points": [strip_bullet_marker(p) for p in s.left.points]}
                    ),
                    "right": s.right.model_copy(
                        update={"points": [strip_bullet_marker(p) for p in s.right.points]}
                    ),
                }
            )
        slides.append(s)
    return gen.model_copy(update={"deck": gen.deck.model_copy(update={"slides": slides})})


# ------------------------------------------------------------------ 페르소나 규칙 사후 검사

# 그림 문자(이모지)로 보는 범위. ✔(U+2714)·☕(U+2615) 같은 기호도 화면에서는 이모지로 보인다.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿⌀-⏿〰〽㊗㊙]")
_NUM_WORDS = {"한": 1, "하나": 1, "두": 2, "둘": 2, "세": 3, "셋": 3, "네": 4, "넷": 4}


def count_emoji(text: str) -> int:
    return len(_EMOJI.findall(text))


def emoji_limit(tone: str) -> int | None:
    """말투 설정에서 이모지 상한을 읽는다: "이모지는 한 개까지" → 1, "이모지 없이" → 0. 없으면 None."""
    if re.search(r"이모지\s*(?:는|를|은)?\s*(?:없이|쓰지\s*않|사용하지\s*않|금지)", tone):
        return 0
    m = re.search(
        r"이모지\s*(?:는|를|은)?\s*(\d+|한|하나|두|둘|세|셋|네|넷)\s*개?\s*(?:까지|이하|이내)", tone
    )
    if not m:
        return None
    v = m.group(1)
    return int(v) if v.isdigit() else _NUM_WORDS[v]


# SNS 글에 어울리지 않는 딱딱한 표현(2026-10-12 스레드 원고 "자료에 따르면")
STIFF_PHRASES = (
    "자료에 따르면",
    "자료에 의하면",
    "수집한 자료",
    "자료에서는",
    "자료가 꼽은",
    "자료에서 짚은",
)


def sns_texts(gen: GeneratedContent) -> dict[str, str]:
    """사람에게 보이는 SNS 글: 캡션, 스레드, 카드 글(장마다). 블로그는 MVP 게시 대상이 아니다(ADR 0016)."""
    out = {"인스타그램 캡션": gen.instagram_caption, "스레드 글": gen.threads_text}
    for i, s in enumerate(gen.deck.slides, start=1):
        parts = [getattr(s, f, "") or "" for f in ("title", "subtitle", "heading", "body", "cta")]
        parts += list(getattr(s, "bullets", []) or [])
        if s.kind == "compare":
            parts += [s.left.title, *s.left.points, s.right.title, *s.right.points]
        out[f"카드 {i}장"] = "\n".join(p for p in parts if p)
    return out


def rule_violations(gen: GeneratedContent, persona: Persona) -> list[str]:
    """프롬프트에만 있던 규칙을 생성 결과에 대고 검사한다. 빈 목록이면 통과."""
    out: list[str] = []
    limit = emoji_limit(persona.tone)
    texts = sns_texts(gen)
    if limit is not None:
        for name, text in texts.items():
            n = count_emoji(text)
            if n > limit:
                out.append(f"{name} 이모지 {n}개(상한 {limit}개)")
    for name in ("인스타그램 캡션", "스레드 글"):
        hit = next((p for p in STIFF_PHRASES if p in texts[name]), None)
        if hit:
            out.append(f'{name}에 딱딱한 표현 "{hit}"')
    return out


# ------------------------------------------------------------------ SNS 글의 출처 줄

_SITE_NAMES = {
    "blog.naver.com": "네이버 블로그",
    "m.blog.naver.com": "네이버 블로그",
    "n.news.naver.com": "네이버 뉴스",
    "news.naver.com": "네이버 뉴스",
    "m.news.naver.com": "네이버 뉴스",
    "cafe.naver.com": "네이버 카페",
    "trends.google.com": "구글 트렌드",
}


def site_label(source: str) -> str:
    """출처 표시: 알려진 사이트는 이름, 그 밖의 URL은 짧은 주소(호스트), URL이 아니면 그대로."""
    if not source.startswith(("http://", "https://")):
        return source.strip()
    host = urlsplit(source).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return _SITE_NAMES.get(host, host)


def sources_line(sources: list[str], n: int = 2) -> str:
    """ "출처: 네이버 블로그, 네이버 뉴스" (서로 다른 것 최대 n개). 출처가 없으면 빈 문자열."""
    labels = list(dict.fromkeys(site_label(s) for s in sources if s and s.strip()))[:n]
    return f"출처: {', '.join(labels)}" if labels else ""
