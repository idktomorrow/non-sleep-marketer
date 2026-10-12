"""플랫폼 어댑터. 실계정 어댑터(배치 G)와 드라이런 어댑터가 같은 capability 표를 쓴다."""

from __future__ import annotations

from nsm.domain.models import Platform
from nsm.ports import CAPABILITIES

# 1일차 검증(docs/day1-verification.md, ADR 0018): 웹 화면에서 할 수 있는 일만 선언한다.
# 인스타그램 웹 댓글 메뉴에는 숨기기가 없다(신고·삭제뿐) → 스팸은 사람 검토로 넘어간다.
PLATFORM_CAPABILITIES: dict[Platform, frozenset[str]] = {
    "threads": CAPABILITIES,
    "instagram": CAPABILITIES - {"hide_comment"},
    "naver_blog": CAPABILITIES - {"hide_comment"},  # MVP 범위 밖(ADR 0016), 검증 안 함
}
