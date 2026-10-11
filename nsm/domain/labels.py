"""법적 표기 문구. 이 파일이 유일한 원본이다 (ADR 0007).

배치 B에서 공정위 추천·보증 심사지침(2026-06-01 시행) 원문과 AI 기본법 시행령을 대조해 확정한다.
"""

VIRTUAL_PERSON_TEXT = "AI를 기반으로 생성된 가상인물이 포함된 게시물입니다"
VIRTUAL_PERSON_BADGE = "가상인물"
AI_IMAGE_CAPTION = "AI 생성 이미지"
AD_HASHTAG = "#광고"
AD_BADGE = "광고"
BLOG_TITLE_SUFFIX = " [가상인물 포함]"

# 페르소나의 1인칭 체험 표현. 가상인물이 실제 사용한 것처럼 쓰면 부당 광고 소지(ADR 0007).
DEFAULT_BANNED_PATTERNS: tuple[str, ...] = (
    r"(직접|제가|내가)\s*(써|사용해|먹어|발라|입어)\s*(봤|보니|본)",
    r"써\s*봤(는데|어요|습니다)",
    r"먹어\s*보니",
    r"(사용|체험)\s*후기",
    r"내돈내산",
)
