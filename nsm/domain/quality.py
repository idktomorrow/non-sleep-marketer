"""콘텐츠 품질 규칙 (배치 Q1, 2026-10-12 드라이런에서 발견한 문제).

프롬프트에만 적어 두면 모델이 어겨도 그대로 나간다. 그래서 코드로 검사하거나 고칠 수 있는 규칙은 여기 둔다.
모두 순수 함수라 엔진·어댑터 어디서든 쓰고, 단위 테스트로 고정한다.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from nsm.domain.models import GeneratedContent, Persona, SourceItem

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


def slide_texts(s: Any) -> list[str]:
    """한 장에 사람 눈에 보이는 글자 전부(장 종류별). 규칙 검사·소품 고르기가 같이 쓴다(S12 리뷰: 한 곳에만)."""
    parts = [getattr(s, f, "") or "" for f in ("title", "subtitle", "heading", "body", "cta")]
    parts += list(getattr(s, "bullets", []) or [])
    if s.kind == "compare":
        parts += [s.left.title, *s.left.points, s.right.title, *s.right.points]
    elif s.kind == "products":
        for it in s.items:
            parts += [it.name, it.price, it.note]
    elif s.kind == "facts":
        for r in s.rows:
            parts += [r.label, r.value]
    return [p for p in parts if p]


def sns_texts(gen: GeneratedContent) -> dict[str, str]:
    """사람에게 보이는 SNS 글: 캡션, 스레드, 카드 글(장마다). 블로그는 MVP 게시 대상이 아니다(ADR 0016)."""
    out = {"인스타그램 캡션": gen.instagram_caption, "스레드 글": gen.threads_text}
    for i, s in enumerate(gen.deck.slides, start=1):
        out[f"카드 {i}장"] = "\n".join(slide_texts(s))
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
        hit = next((p for p in UNCONFIRMED_PHRASES if p in text), None)
        if hit:
            out.append(f'{name}에 확인되지 않은 내용 "{hit}"')
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def unverified_products(gen: GeneratedContent, sources: list[SourceItem]) -> list[str]:
    """상품 그리드(S12)의 이름·가격이 자료 본문에 없으면 사유 목록. 가격·상품명은 틀리면 바로 민원이 되므로 검토로."""
    hay = _squash(" ".join(f"{s.title} {s.text}" for s in sources))
    # 가격은 숫자 묶음 단위로 정확히 맞아야 한다("¥2,310"이 "¥12,310"의 일부로 맞지 않게, S12 리뷰)
    numbers = set(re.findall(r"\d+", re.sub(r"(?<=\d)[,.](?=\d{3})", "", hay)))
    out: list[str] = []
    for slide in gen.deck.slides:
        if slide.kind != "products":
            continue
        for it in slide.items:
            name = _squash(it.name)
            if name and name not in hay:
                out.append(f"상품 '{it.name}'이(가) 자료에 없음")
            price = re.sub(r"[^0-9]", "", it.price)
            if price and price not in numbers:
                out.append(f"'{it.name}' 가격 {it.price}이(가) 자료에 없음")
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
