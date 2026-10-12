# 0009. 게시 상태를 콘텐츠가 아니라 플랫폼별 게시 레코드(publications)에 둔다

- 상태: 채택(2026-10-13, 배치 B 반영 PR에서 선웅제 확정) · 날짜: 2026-10-11 · 관련: 이슈 #2, [contracts 2절](../contracts.md)

## 배경

계약 초안은 콘텐츠 하나에 `approved → publishing → published | failed → abandoned` 상태를 두었다. 그런데 콘텐츠 하나는 인스타그램·스레드·네이버 블로그 세 곳에 서로 다른 시각(게시 슬롯)에 올라간다. 스레드는 성공하고 인스타그램은 실패할 수 있다. 상태 하나로는 이를 표현할 수 없다.

## 선택지

1. 콘텐츠 상태 하나 유지: 단순 / 플랫폼별 성공·실패·재시도를 표현 못 함
2. **콘텐츠 상태(검토 흐름) + 플랫폼별 게시 레코드 상태(게시 흐름)로 분리**

## 결정

2번.
- 콘텐츠: `draft → pending_review → approved → published | discarded` (모든 대상 플랫폼 게시 완료 시 published).
- 게시 레코드(`publications`, 콘텐츠×플랫폼): `queued → publishing → published | failed → (재시도) → abandoned`.

## 결과

- 재시도·멱등성(재시작 시 `publishing` 잔여)은 게시 레코드 단위로 판단한다. abandoned나 중단된 레코드가 생기면 콘텐츠는 사유와 함께 `pending_review`로 돌아간다.
- `ContentStatus`에서 publishing/failed/abandoned를 빼고 `PublicationStatus`를 추가했다(`nsm/domain/models.py`).
