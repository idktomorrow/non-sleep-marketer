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
| [0004](0004-three-platforms-in-mvp.md) | 인스타그램·스레드·네이버 블로그를 MVP부터 | 채택 |
| [0005](0005-dry-run-first.md) | 드라이런 MVP를 먼저 | 채택 |
| [0006](0006-single-process-duckdb-fastapi.md) | 단일 프로세스 + DuckDB + FastAPI 대시보드 | 채택 |
| [0007](0007-labels-and-no-testimonials.md) | 가상인물·광고 표기 규칙과 후기형 제외 | 채택 |
| [0008](0008-autopublish-guardrails.md) | 자동 게시 안전장치(보호 모드·쿼터·비용 상한·claim 출처) | 채택 |
