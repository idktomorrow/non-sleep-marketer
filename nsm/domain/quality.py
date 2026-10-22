"""콘텐츠 품질 규칙 (배치 Q1, 2026-10-12 드라이런에서 발견한 문제).

프롬프트에만 적어 두면 모델이 어겨도 그대로 나간다. 그래서 코드로 검사하거나 고칠 수 있는 규칙은 여기 둔다.
모두 순수 함수라 엔진·어댑터 어디서든 쓰고, 단위 테스트로 고정한다.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from nsm.domain.models import GeneratedContent, Persona

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ 관련성: 제외 장르

# 방문 후기·여행기·개인 일기. 정보성 글이 아니고, 후기형은 공정위 지침 위험으로 쓰지 않는다(ADR 0007).
_EXCLUDED_GENRE = re.compile(
    r"내돈내산|방문\s*후기|방문기|다녀왔어|다녀왔습|다녀온\s*(?:후기|곳|카페|맛집)|"
    r"여행기|여행\s*(?:후기|일기|중에|브이로그)|카페\s*투어|투어\s*후기|브이로그|vlog|"
    r"(?:육아|하루|여행|카페|커피|일상|다이어트|홈카페)\s*일기|(?:오늘|나|우리)\s*의?\s*일기|일기\s*(?:장|쓰기)|일상\s*(?:기록|브이로그)|"
    r"데이트\s*(?:코스|후기)|(?:구매|사용|솔직|체험|시음|맛집|카페|방문)\s*(?:후기|리뷰)|후기\s*[♥❤!]|"
    r"웨이팅\s*(?:후기|있|길|필수)",
    re.IGNORECASE,
)


def excluded_genre(title: str, text: str) -> str | None:
    """제목(과 본문 앞부분)이 후기·여행기·일기 같으면 걸린 말을 돌려준다. 분류기를 부르기 전에 거른다."""
    m = _EXCLUDED_GENRE.search(f"{title}\n{text[:120]}")
    return m.group(0) if m else None


# ------------------------------------------------------------------ 카드 목록 번호

_BULLET_MARK = re.compile(r"^\s*(?:\d{1,2}\s*[.)](?=\s|$)|[①-⑳]|[-•·*▪︎✔✅☑]️?(?=\s))\s*")


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


_EMOJI_MODIFIERS = re.compile("[\U0001f3fb-\U0001f3ff\ufe0f]")
_ZWJ_SEQ = re.compile(f"{_EMOJI.pattern}(?:\u200d{_EMOJI.pattern})+")
_FLAG = re.compile("[\U0001f1e6-\U0001f1ff]{2}")
_KEYCAP = re.compile("[0-9#*]\u20e3")


def count_emoji(text: str) -> int:
    """화면에 보이는 이모지 개수: 👍🏻·🇰🇷·👨‍👩‍👧는 1개, 1️⃣ 같은 키캡도 1개."""
    t = _EMOJI_MODIFIERS.sub("", text)
    n = len(_KEYCAP.findall(t))
    t = _KEYCAP.sub("", t)
    n += len(_ZWJ_SEQ.findall(t))
    t = _ZWJ_SEQ.sub("", t)
    n += len(_FLAG.findall(t))
    t = _FLAG.sub("", t)
    return n + len(_EMOJI.findall(t))


def emoji_limit(tone: str) -> int | None:
    """말투 설정에서 이모지 상한을 읽는다: "이모지는 한 개까지" → 1, "이모지 없이" → 0. 없으면 None."""
    if re.search(r"이모지\s*(?:는|를|은)?\s*(?:없이|쓰지\s*않|사용하지\s*않|금지)", tone):
        return 0
    num = r"(\d+|한|하나|두|둘|세|셋|네|넷)"
    m = re.search(
        rf"이모지\s*(?:는|를|은)?\s*(?:최대\s*)?(?:{num}\s*(?:~|-|에서)\s*)?{num}\s*개?\s*"
        r"(?:까지|이하|이내|정도|만)?(?![0-9])",
        tone,
    ) or re.search(rf"이모지\s*(?:는|를|은)?\s*최대\s*{num}\s*개?", tone)
    if not m:
        if "이모지" in tone:
            log.info("말투 설정의 이모지 규칙을 읽지 못함(검사 안 함): %s", tone)
        return None
    v = m.groups()[-1]  # 범위("1~2개")면 큰 쪽이 상한
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


# 자료가 빈약할 때 모델이 칸을 채우는 일반론(2026-10-22 "달력 어디서 살까요 → 검색해 보세요" 원고, C31).
# "확인해 보세요"·"참고하세요"는 정상 마무리 문구에도 흔해 넣지 않는다(리뷰).
FILLER_PHRASES = (
    "검색해 보세요",
    "검색해보세요",
    "검색하세요",
    "검색해 비교",
    "판매자마다 달라",
    "판매처마다 달라",
    "찾아보세요",
)

# 구체 사실로 세는 것: 날짜, 숫자(수량·가격·순위), 장소·판매처 꼬리, 따옴표 이름, 유명 브랜드.
# 한글 뒤에는 조사가 붙으므로(다이소에서, 팝업스토어에) 낱말 경계(\b)를 쓰지 않는다.
_FACT_DATE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}월\s*\d{1,2}일|\d{4}년")
_FACT_NUMBER = re.compile(
    r"\d[\d,.]*\s*(?:만|천|억|원|엔|달러|%|개|명|일|월|시|분|장|호|관|종|권|회|위|주|배|cm|mm|kg|g|ml|l|dx)?",
    re.I,
)
_FACT_PLACE = re.compile(
    r"[가-힣A-Za-z0-9]+(?:스토어|샵|숍|몰|백화점|센터|극장|영화관|편의점|마켓|마트|팝업|스퀘어|타워|플라자|공항|호텔|카페)"
)
_FACT_NAME = re.compile(r"[「『'\"]([^「」『』'\"]{2,30})[」』'\"]")
_FACT_BRAND = re.compile(
    r"(?<![A-Za-z0-9])(?:CGV|롯데시네마|메가박스|다이소|올리브영|무신사|쿠팡|네이버쇼핑|29CM|카카오|미니소|GS25|CU|세븐일레븐|이마트|홈플러스|코엑스|더현대|롯데월드|에버랜드|ANA|JAL)(?![A-Za-z0-9])",
    re.I,
)


def fact_count(gen: GeneratedContent) -> int:
    """원고(사실 주장 + 카드 글)에 든 서로 다른 구체 사실의 수(C31).
    날짜 하나는 사실 하나(2026-10-25가 셋으로 안 세게 먼저 떼어 낸다). 한 자리 숫자(글머리 "1.")는 안 센다."""
    texts = [c.text for c in gen.claims] + list(sns_texts(gen).values())
    found: set[str] = set()
    for t in texts:
        for m in _FACT_DATE.finditer(t):
            found.add("date:" + " ".join(m.group(0).split()))
        rest = _FACT_DATE.sub(" ", t)
        for m in _FACT_NUMBER.finditer(rest):
            token = " ".join(m.group(0).split()).lower()
            digits = sum(ch.isdigit() for ch in token)
            unit = token.rstrip("0123456789.,").strip()
            if digits < 2 and not unit:
                continue  # "1." 같은 글머리·한 자리 숫자
            found.add("num:" + token)
        for rx in (_FACT_PLACE, _FACT_NAME, _FACT_BRAND):
            for m in rx.finditer(rest):
                found.add(" ".join(m.group(0).split()).lower())
    return len(found)


_TITLE_TOKEN = re.compile(r"[가-힣a-zA-Z0-9]{2,}")
# 어느 페르소나에나 흔한 낱말. 페르소나의 관심 주제 낱말은 호출부가 stop으로 더 넘긴다(material.py)
TITLE_STOP = {
    "소식",
    "정보",
    "정리",
    "총정리",
    "안내",
    "관련",
    "이야기",
    "그리고",
    "에서",
    "으로",
    "추천",
    "방법",
}


def title_tokens(text: str, stop: set[str] | None = None) -> set[str]:
    """제목·주장의 낱말 집합(같은 사건 판정용). 흔한 낱말과 주제 낱말(stop)은 뺀다."""
    drop = TITLE_STOP | (stop or set())
    return {t.lower() for t in _TITLE_TOKEN.findall(text) if t.lower() not in drop}


def same_event(a: set[str], b: set[str], threshold: float = 0.5) -> bool:
    """두 낱말 집합이 같은 사건을 가리키나(자카드 겹침). 낱말이 두 개 이하면 완전 일치일 때만."""
    if not a or not b:
        return False
    if min(len(a), len(b)) < 3:
        return a == b  # 낱말이 한두 개면 포함 관계로는 못 믿는다("원두" ⊂ 아무 원두 글)
    return len(a & b) / len(a | b) >= threshold


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


# 확인되지 않은 소문·추측을 SNS 글에 옮긴 흔적(ADR 0032). 실제 고객 후기 인용("고객 후기에 따르면")과는 다르다.
UNCONFIRMED_PHRASES = (
    "소문",
    "루머",
    "카더라",
    "이야기가 돌",
    "얘기가 돌",
    "예상 가격",
    "아직 공지",
    "아직 공개되지",
)


# 카드 마무리 문구에 쓰면 어색한 말: 카드 이미지는 누를 수 없다(2026-10-13 "링크는 저장부터 해요").
CTA_AWKWARD = ("링크", "출처", "URL", "url", "프로필", "클릭", "눌러")


def outro_cta(gen: GeneratedContent) -> str:
    return next((s.cta for s in gen.deck.slides if s.kind == "outro"), "")


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
    hit = next((p for p in CTA_AWKWARD if p in outro_cta(gen)), None)
    if hit:
        out.append(f'마무리 문구에 카드에서 할 수 없는 말 "{hit}"')
    for name, text in texts.items():
        hit = next((p for p in FILLER_PHRASES if p in text), None)
        if hit:
            out.append(f'{name}에 자료 없이 채운 일반론 "{hit}"')
    for name, text in texts.items():
        hit = next((p for p in UNCONFIRMED_PHRASES if p in text), None)
        if hit:
            out.append(f'{name}에 확인되지 않은 내용 "{hit}"')
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
    # 주요 언론사(하위 도메인도 같은 이름: ent.sbs.co.kr → SBS)
    "yna.co.kr": "연합뉴스",
    "yonhapnewstv.co.kr": "연합뉴스TV",
    "news1.kr": "뉴스1",
    "newsis.com": "뉴시스",
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보",
    "kmib.co.kr": "국민일보",
    "seoul.co.kr": "서울신문",
    "munhwa.com": "문화일보",
    "segye.com": "세계일보",
    "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제",
    "sedaily.com": "서울경제",
    "edaily.co.kr": "이데일리",
    "mt.co.kr": "머니투데이",
    "heraldcorp.com": "헤럴드경제",
    "asiae.co.kr": "아시아경제",
    "fnnews.com": "파이낸셜뉴스",
    "inews24.com": "아이뉴스24",
    "etnews.com": "전자신문",
    "zdnet.co.kr": "지디넷코리아",
    "sbs.co.kr": "SBS",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "jtbc.co.kr": "JTBC",
    "ytn.co.kr": "YTN",
    "dailian.co.kr": "데일리안",
    "tvreport.co.kr": "TV리포트",
    "topstarnews.net": "톱스타뉴스",
    "isplus.com": "일간스포츠",
    "sportschosun.com": "스포츠조선",
    "osen.co.kr": "OSEN",
    "xportsnews.com": "엑스포츠뉴스",
    "mydaily.co.kr": "마이데일리",
    "newsen.com": "뉴스엔",
    "ize.co.kr": "아이즈",
    "joynews24.com": "조이뉴스24",
    "tf.co.kr": "더팩트",
    "kukinews.com": "쿠키뉴스",
    "mediapen.com": "미디어펜",
    "ggilbo.com": "금강일보",
    "slist.kr": "스포츠W",
}


def site_label(source: str) -> str:
    """출처 표시: 알려진 사이트는 이름, 그 밖의 URL은 짧은 주소(호스트), URL이 아니면 그대로."""
    if not source.startswith(("http://", "https://")):
        return source.strip()
    host = urlsplit(source).netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if host in _SITE_NAMES:
        return _SITE_NAMES[host]
    # 하위 도메인: ent.sbs.co.kr → sbs.co.kr, news.jtbc.co.kr → jtbc.co.kr
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in _SITE_NAMES:
            return _SITE_NAMES[parent]
    return host


def card_sources(sources: list[str], n: int = 6) -> list[str]:
    """카드 마무리 장에 보일 출처: 긴 주소 대신 사이트 이름(같은 이름은 한 번), 최대 n개.
    원문 주소는 원고 데이터와 대시보드 "사실 주장과 출처"에 그대로 남는다(ADR 0023 보충)."""
    return list(dict.fromkeys(site_label(s) for s in sources if s and s.strip()))[:n]


def sources_line(sources: list[str], n: int = 2) -> str:
    """ "출처: 네이버 블로그, 네이버 뉴스" (서로 다른 것 최대 n개). 출처가 없으면 빈 문자열."""
    labels = list(dict.fromkeys(site_label(s) for s in sources if s and s.strip()))[:n]
    return f"출처: {', '.join(labels)}" if labels else ""
