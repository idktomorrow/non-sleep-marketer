# ADR (Architecture Decision Record)

설계 결정 하나를 한 쪽짜리 문서로 남긴 것입니다. "무엇을 했나"는 코드와 커밋이 말해 주지만 "왜 그렇게 했나, 다른 선택지는 무엇이었나"는 코드에 남지 않습니다. ADR이 그 빈칸을 채웁니다.

## 규칙

- 파일명: `NNNN-짧은-영문-slug.md`, 번호는 증가만 합니다.
- 상태: `제안`(배치 중 Claude가 기본값으로 고른 것, PR 리뷰에서 확정) → `채택` / `기각`. 결정이 바뀌면 원래 ADR을 고치지 않고 새 ADR을 쓰고 상태를 `대체됨(→ NNNN)`으로 바꿉니다.
- 템플릿: [0000-template.md](0000-template.md)

## 목록

| 번호 | 제목 | 상태 |
|---|---|---|
| [0001](0001-local-browser-automation.md) | 사용자별 로컬 설치 + 브라우저 자동화(공식 API 미사용) | 채택 |
| [0002](0002-aside-default-driver-split.md) | Aside 기본 드라이버, Playwright 대안, 드라이버 분리 | 채택 |
| [0003](0003-classifier-jev-and-claude.md) | 분류기는 MotherDuck jev, Claude를 동등 구현으로 | 채택 |
| [0004](0004-three-platforms-in-mvp.md) | 인스타그램·스레드·네이버 블로그를 MVP부터 | 대체됨(0016) |
| [0005](0005-dry-run-first.md) | 드라이런 MVP를 먼저 | 채택 |
| [0006](0006-single-process-duckdb-fastapi.md) | 단일 프로세스 + DuckDB + FastAPI 대시보드 | 채택 |
| [0007](0007-labels-and-no-testimonials.md) | 가상인물·광고 표기 규칙과 후기형 제외 | 채택 |
| [0008](0008-autopublish-guardrails.md) | 자동 게시 안전장치(보호 모드·쿼터·비용 상한·claim 출처) | 채택 |
| [0009](0009-per-platform-publication-state.md) | 게시 상태를 플랫폼별 게시 레코드에 둔다 | 채택 |
| [0010](0010-collect-driver-default-playwright.md) | 배치 B 전까지 드라이버 기본값 Playwright | 대체됨(0017) |
| [0011](0011-keyword-classifier-fallback.md) | 키가 없으면 키워드 분류기로 대체 | 채택 |
| [0012](0012-daily-target-and-topic-rotation.md) | 하루 목표·플랫폼 배정·주제 순환 | 채택 |
| [0013](0013-autostart-startup-folder.md) | 자동 실행은 시작프로그램 폴더로 | 채택 |
| [0014](0014-rule-based-weekly-report.md) | 주간 리포트는 규칙 계산, 기간은 현지 날짜 지난 7일 | 채택 |
| [0015](0015-main-takes-s-alpha.md) | S-α(템플릿·렌더러)를 메인이 맡는다 | 채택 |
| [0016](0016-drop-naver-blog-publishing.md) | 네이버 블로그 게시를 MVP 범위에서 뺀다 | 채택 |
| [0017](0017-aside-default-driver-after-day1.md) | 1일차 검증 후 브라우저 드라이버 기본값을 Aside로 | 채택 |
| [0018](0018-platform-capabilities-from-day1.md) | 플랫폼 capability: 스레드 숨기기 지원, 인스타그램 미지원 | 채택 |
| [0019](0019-label-wording-and-placement.md) | 표기 문구는 공정위 예규 원문, 가상인물 배지는 이름 옆 | 채택 |
| [0020](0020-aside-repl-execution.md) | 실계정 어댑터는 `aside repl` 고정 스크립트, 이미지는 일회용 로컬 서버 | 채택 |
| [0021](0021-collect-driver-stays-playwright.md) | 자료 수집은 Playwright 수집 전용 프로필 유지(게시·댓글만 Aside) | 채택 |
| [0022](0022-fetch-article-body-for-relevant-items.md) | 관련 자료로 판정된 글만 원문 본문(상한 3,000자) | 채택 |
| [0023](0023-code-checked-content-rules.md) | 코드로 검사할 수 있는 원고 규칙은 생성 후 검사, 출처 줄은 게시 때 | 채택 |
| [0024](0024-card-editor-scope.md) | 카드 편집 화면: 문구·배경·미리보기·출처, 스타일 선택은 S8 뒤로 | 채택 |
| [0025](0025-dashboard-design.md) | 대시보드 디자인: 오늘 할 일 중심, 밝은 관리 화면(취향 선택 3가지 모두 A안) | 채택 |
| [0026](0026-skip-blog-draft.md) | 블로그를 게시하지 않으면 블로그 원고를 만들지 않음(필드는 빈 값으로 유지) | 채택 |
| [0027](0027-posting-hours.md) | 일은 24시간, 게시는 게시 시간(`posting_hours`)에만 | 채택 |
| [0028](0028-dashboard-settings.md) | 대시보드 설정 화면: 페르소나·브랜드·운영은 화면에서, 계정·드라이런·키는 `nsm init`에서만 | 채택 |
| [0029](0029-reply-interval.md) | 댓글 확인 간격은 설정값(기본 5분, 3~60분) | 채택 |
| [0030](0030-notifications-and-manual-runs.md) | 알림 앱 ID 등록·켜기/끄기, 지금 실행 진행 표시, 원고 만들기는 누를 때마다 1건 더 | 채택 |
