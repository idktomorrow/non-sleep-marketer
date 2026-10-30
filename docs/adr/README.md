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
| [0003](0003-classifier-jev-and-claude.md) | 분류기는 MotherDuck jev, Claude를 동등 구현으로 | 대체됨(0048) |
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
| [0019](0019-label-wording-and-placement.md) | 표기 문구는 공정위 예규 원문, 가상인물 배지는 이름 옆(어느 장에 둘지는 0043) | 채택 |
| [0020](0020-aside-repl-execution.md) | 실계정 어댑터는 `aside repl` 고정 스크립트, 이미지는 일회용 로컬 서버 | 채택 |
| [0021](0021-collect-driver-stays-playwright.md) | 자료 수집은 Playwright 수집 전용 프로필 유지(게시·댓글만 Aside) | 채택 |
| [0022](0022-fetch-article-body-for-relevant-items.md) | 관련 자료로 판정된 글만 원문 본문(상한 3,000자) | 채택 |
| [0023](0023-code-checked-content-rules.md) | 코드로 검사할 수 있는 원고 규칙은 생성 후 검사, 출처 줄은 게시 때 | 채택 |
| [0024](0024-card-editor-scope.md) | 카드 편집 화면: 문구·배경·미리보기·출처, 스타일 선택은 S8 뒤로 | 채택 |
| [0025](0025-dashboard-design.md) | 대시보드 디자인: 오늘 할 일 중심, 밝은 관리 화면(취향 선택 3가지 모두 A안) | 채택 |
| [0026](0026-skip-blog-draft.md) | 블로그를 게시하지 않으면 블로그 원고를 만들지 않음(필드는 빈 값으로 유지) | 채택 |
| [0027](0027-posting-hours.md) | 일은 24시간, 게시는 게시 시간(`posting_hours`)에만 | 채택 |
| [0028](0028-dashboard-settings.md) | 대시보드 설정 화면: 페르소나·브랜드·운영은 화면에서, 계정·드라이런·보호 모드는 `nsm init`에서만. 개정 1(C35): API 키는 화면에서(keyring만, 확인 버튼) | 채택(개정 1 제안) |
| [0029](0029-reply-interval.md) | 댓글 확인 간격은 설정값(기본 5분, 3~60분) | 채택 |
| [0030](0030-notifications-and-manual-runs.md) | 알림 앱 ID 등록·켜기/끄기, 지금 실행 진행 표시, 원고 만들기는 누를 때마다 1건 더 | 채택 |
| [0031](0031-notification-inbox-and-publish-queue.md) | 알림함(종 모양, DB 보관), 진행 표시 닫기, 게시 대기 화면(검토로 되돌리기·폐기) | 채택 |
| [0032](0032-fresh-sources-and-no-rumors.md) | 네이버 검색 최신순·기간, 원고에 오늘·자료 날짜, 지난 예정·소문 금지와 코드 검사 | 채택 |
| [0033](0033-materials-page.md) | 자료 화면: 파일 올리기(바로 읽기 확인), 올린 파일 상태, 모은 자료와 판정 | 채택 |
| [0034](0034-main-takes-s-beta-gamma-s8.md) | 2주차 서브 배치 S-β(#21)·S-γ(#23)·S8(#34)을 메인이 맡음(머지 후 서브가 다듬을 수 있음) | 채택 |
| [0035](0035-cover-readable-at-threads-thumbnail.md) | 카드 표지 글자를 스레드 피드 썸네일(224×280 실측) 크기에서도 읽히게 키우고, 제목의 빈 줄은 합침 | 채택 |
| [0036](0036-code-drawn-card-styles.md) | 카드 스타일 clean·photo·illust(`brand.card_style`)는 코드로 그린 배경·자체 캐릭터만, AI 배경 이미지는 기본 off | 채택 |
| [0037](0037-per-content-card-style.md) | 원고별 카드 스타일은 덱 JSON `style` 필드 계약 변경으로 2차에(합의 PR) | 제안 |
| [0038](0038-instagram-rate-limit-rest.md) | 플랫폼 요청 제한(HTTP 429)은 로그인 문제와 구분해 2~8시간 쉬고, 인스타그램 댓글은 30분마다·최근 3일 글만 | 제안 |
| [0039](0039-readable-run-summaries.md) | 작업 요약은 한 함수(`summary.describe`)가 만드는 한국어 문장, DB의 예전 형식 줄은 보여 줄 때 바꿈 | 제안 |
| [0040](0040-run-log-shows-changes.md) | 실행 기록 화면은 작업 상태 표 + 변화가 있던 실행만(같은 결과는 묶음), 전체는 버튼으로 | 제안 |
| [0041](0041-stability-watchdog-and-backup.md) | 안정화: "계속 0건"(2일)은 health 잡이 확인해 오늘 할 일·하루 한 번 알림, 매일 새벽 `COPY FROM DATABASE` 백업(최근 7개), 실행 기록 90일 보관 | 제안 |
| [0042](0042-simple-and-advanced-screen-modes.md) | 대시보드는 한 앱 안의 "간단(기본)·고급" 스위치로 보이는 것만 줄인다. 간단은 자료·실행 기록 메뉴와 세부 설정을 숨기고 "오늘"에 자료 올리기 | 제안 |
| [0043](0043-virtual-person-badge-cover-and-outro.md) | 카드의 이름과 "가상인물" 배지는 표지와 마지막 장에만, 중간 장에는 둘 다 없음(이름만 남기지 않음) | 채택 |
| [0044](0044-keyword-drafts-and-exclude-words.md) | 키워드로 바로 찾아 원고 1건(이번에 찾은 자료만, 평소 게시 규칙, 자료 없으면 만들지 않음), 수집 제외어(모으지도 쓰지도 않음, 키워드에 든 제외어는 그때만 풀림) | 제안 |
| [0045](0045-image-materials.md) | 올린 이미지(png·jpg·webp)는 Claude가 보이는 글자·사실만 옮겨 자료로 쓴다(한 번만 읽고 캐시, 정보 없으면 거절). 카드에는 이미지를 넣지 않음 | 제안 |
| [0046](0046-persona-character-images.md) | 일러스트형 카드 캐릭터는 설정 `brand.character`(기본 앵무새, 햄스터, 직접 올린 그림, 도형, 없음). 올린 캐릭터는 바탕을 자동으로 지우고, 로고도 설정 화면에서 올림. AI로 만든 그림이면 그 장에 "AI 생성 이미지"(기본 켬) | 제안 |
| [0047](0047-replies-grounded-in-post-sources.md) | 댓글 답글은 게시물 본문 + 그 원고에 쓴 자료(최대 3개, 원문 앞 1500자)를 보고 쓴다. 자료에 없으면 지어내지 않되 "모르겠어요"로 딱 자르지 않고 아는 만큼 + "아직 발표 전인 것 같아요". 댓글마다 새 검색은 하지 않음 | 제안 |
| [0057](0057-rag-search-and-index-job.md) | 문서 검색(같은 모델로 질문 임베딩, 전수 코사인, 기준값 0.65 아래는 "근거 없음")과 색인 잡(nsm run 안, 매일 20:00, 하루 900조각, 키 없음·한도면 조용히) | 제안 |
| [0056](0056-rag-chunk-index.md) | 문서 근거 챗봇 1단계: `nsm/rag/` 자체 포트, 제목 단위 500자 조각(큰 표는 행 경계·머리글), Gemini `gemini-embedding-2` 768차원, `doc_chunks` FLOAT[], 비용은 classifier·ref embedding | 제안 |
| [0055](0055-incident-view-and-diagnosis.md) | 장애 화면(failures 표·지금 상태·스크린샷)과 화면 변경 자동 진단(6시간·하루 5회·비용 상한, 데이터 경계). 진단은 글만, 수정은 사람 | 제안 |
| [0054](0054-error-taxonomy-and-evidence.md) | 오류 종류 표(일시적·요청 제한·로그인·화면 변경·설정/버그)로 실패 처리, 실패 순간 증거 저장. 화면 변경은 재시도하지 않고 바로 검토로. 코드가 스스로 고치지는 않음 | 제안 |
| [0053](0053-short-label-and-feed-hook.md) | 첫 줄 표기를 지침 예시 "가상인물 포함" 기반 13자로 줄여 피드 미리보기(125자)에 핵심이 들어가게 | 제안 |
| [0052](0052-comment-triage.md) | 사람 칸에는 돈·약속이 걸린 댓글만: 불만은 브랜드/소식 반응으로 2차 판정, 숨기기 없는 플랫폼의 스팸은 자동 처리 | 제안 |
| [0051](0051-content-quality-floor.md) | 원고 품질 하한: 빈약한 자료·이미 올린 소식·소재 쏠림을 재료 단계에서 거르고, 구체 사실 없는 원고는 자동 승인하지 않음 | 제안 |
| [0050](0050-google-news-rss-source.md) | 구글 뉴스 RSS를 주제별 기사 소스로 추가(트렌드는 유지). 주소 풀기는 관련 자료만, 제목 겹침 중복 제거 | 제안 |
| [0049](0049-presets-as-workspaces.md) | 프리셋은 작업 공간: 프리셋마다 DB 파일, 계정·키·운영 설정은 공통, 전환은 작업 스레드에서 | 제안 |
| [0048](0048-gemini-classifier-replaces-jev.md) | 분류기는 Gemini 무료 구간(Flash-Lite, 10건 묶음)을 기본으로, jev·MotherDuck은 제거. 자동 선택 Gemini → Claude → 낱말 규칙, 실패하면 다음으로 넘어가며 알림 | 제안 |
