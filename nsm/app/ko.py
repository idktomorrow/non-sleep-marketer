"""대시보드에 보이는 내부 값(영어)을 한국어로 (배치 I).

DB·라우트·계약의 값은 그대로 두고 화면에서만 바꾼다. 모르는 값은 원래 값을 그대로 보여 준다.
"""

from __future__ import annotations

CONTENT_STATUS = {
    "draft": "작성 중",
    "pending_review": "검토 대기",
    "approved": "승인됨(게시 대기)",
    "published": "게시됨",
    "discarded": "폐기됨",
}
PUB_STATUS = {
    "queued": "게시 대기",
    "publishing": "올리는 중",
    "published": "게시됨",
    "failed": "실패(다시 시도 예정)",
    "abandoned": "포기(검토로 돌아옴)",
}
TEMPLATE = {"info": "정보형", "list": "리스트형", "compare": "비교형"}
SLIDE_KIND = {"cover": "표지", "body": "본문", "list": "목록", "compare": "비교", "outro": "마무리"}
PLATFORM = {"instagram": "인스타그램", "threads": "스레드", "naver_blog": "네이버 블로그"}
COMMENT_CATEGORY = {
    "praise_general": "칭찬·일반",
    "question": "질문",
    "complaint": "불만",
    "price_refund": "가격·환불",
    "legal": "법적 문제",
    "spam": "스팸 추정",
}
COMMENT_STATUS = {
    "new": "새 댓글",
    "auto_replied": "자동 답글",
    "hidden": "숨김",
    "escalated": "확인 필요",
    "replying": "답글 보내는 중",
    "ignored": "넘김",
    "resolved": "처리 완료",
}
COST_KIND = {"llm": "글 생성", "image": "배경 이미지", "classifier": "자료 분류"}

# 잡 이름 → (짧은 이름, 하는 일). "지금 실행" 버튼과 실행 기록에 쓴다.
JOBS: dict[str, tuple[str, str]] = {
    "collect": (
        "자료 모으기",
        "네이버·구글 트렌드·올린 파일에서 자료를 모으고 관련 있는 것만 고릅니다",
    ),
    "generate": (
        "원고 만들기",
        "누를 때마다 원고 1건을 더 만듭니다(오늘 목표와 별개, Claude 약 3~6센트)",
    ),
    "publish": ("게시하기", "승인된 원고를 올립니다(드라이런이면 미리보기 폴더에 저장)"),
    "reply": ("댓글 확인", "새 댓글을 읽고 답하거나 사람에게 넘깁니다"),
    "metrics": ("성과 읽기", "게시물의 조회·좋아요 같은 지표를 모읍니다"),
    "health": ("로그인 점검", "플랫폼 로그인 상태를 확인합니다"),
    "weekly_report": ("주간 리포트", "지난 7일 성과를 정리합니다"),
}


# 작업 결과 숫자 → 한국어(지금 실행 결과 알림에 쓴다). 여기 없는 숫자는 보여 주지 않는다.
RESULT_COUNTS = {
    "new": "새 자료",
    "relevant": "관련 자료",
    "excluded_genre": "후기·일기 제외",
    "created": "새 원고",
    "pending_review": "검토 대기",
    "approved": "바로 승인",
    "published": "게시",
    "failed": "실패",
    "auto_replied": "자동 답글",
    "hidden": "숨김",
    "escalated": "사람 확인",
}


def result_text(result: object) -> str:
    """JobResult → "새 자료 15건 · 관련 자료 5건" 같은 한 줄. 결과가 JobResult가 아니면 "끝"."""
    skipped = getattr(result, "skipped", None)
    counts = getattr(result, "counts", None)
    if counts is None:
        return "끝났습니다"
    if skipped:
        return f"건너뜀: {skipped}"
    parts = [
        f"{label} {counts[k]}건"
        for k, label in RESULT_COUNTS.items()
        if isinstance(counts.get(k), int) and not isinstance(counts.get(k), bool)
    ]
    errors = getattr(result, "errors", None) or []
    text = " · ".join(parts) or "바뀐 것 없음"
    waits = counts.get("waits")
    if isinstance(waits, list) and waits and not counts.get("published"):
        text += " / " + ", ".join(waits)  # 게시: 왜 안 올렸는지
    return f"{text} · 오류 {len(errors)}건: {errors[0]}"[:300] if errors else text


def ko(value: object, table: dict[str, str]) -> str:
    if value is None:
        return ""
    return table.get(str(value), str(value))


FILTERS = {
    "ko_status": lambda v: ko(v, CONTENT_STATUS),
    "ko_pub": lambda v: ko(v, PUB_STATUS),
    "ko_template": lambda v: ko(v, TEMPLATE),
    "ko_kind": lambda v: ko(v, SLIDE_KIND),
    "ko_platform": lambda v: ko(v, PLATFORM),
    "ko_category": lambda v: ko(v, COMMENT_CATEGORY),
    "ko_comment": lambda v: ko(v, COMMENT_STATUS),
    "ko_cost": lambda v: ko(v, COST_KIND),
    "ko_job": lambda v: JOBS.get(str(v), (str(v), ""))[0],
}
