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
| [0028](0028-dashboard-settings.md) | 대시보드 설정 화면: 페르소나·브랜드·운영은 화면에서, 계정·드라이런·키는 `nsm init`에서만 | 채택 |
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
| [0049](0049-presets-as-workspaces.md) | 프리셋은 작업 공간: 프리셋마다 DB 파일, 계정·키·운영 설정은 공통, 전환은 작업 스레드에서 | 제안 |
| [0048](0048-gemini-classifier-replaces-jev.md) | 분류기는 Gemini 무료 구간(Flash-Lite, 10건 묶음)을 기본으로, jev·MotherDuck은 제거. 자동 선택 Gemini → Claude → 낱말 규칙, 실패하면 다음으로 넘어가며 알림 | 제안 |
| [0050](0050-editorial-style-verified-photos.md) | 사진 우선형(editorial) 카드 스타일 추가: 사람이 올리거나 확인한 사진만 장별 배경으로 크게 깔고 글은 그 위 패널에. 프로그램이 고른 사진은 넣지 않음(ADR 0045 수정 제안) | 제안 |
| [0051](0051-daily-picks-and-saved-drafts.md) | 오늘의 추천: 켜면 일일 원고 대신 수집→사건 묶기→최근 소식 최대 3건을 검토 대기 원고로. `picks` 표로 보관·사진 후보·재확인을 다루고(동결 상태 그대로), 사진 후보는 사람이 "쓰기"를 눌러야 카드에 | 제안 |
| [0058](0058-cast-and-persistent-image-jobs.md) | 출연진(주인공·보조, 정체성 출처 account/content/custom × 생성 여부, 제작 당시 참조 복사본)·작업 공간별 캐릭터 프로필, 영속 그림 작업(호출 전 행, 슬롯별 spec 지문으로 유효성, 2컷 의존, 응답 불명 무재시도, 재시작 복구), 그림 이력(재사용·직접 올리기) | 제안 |
| [0059](0059-approved-artifacts-from-preview.md) | 승인 = 사용자가 본 미리보기 판(판 번호·지문·PNG 해시)을 임시 경로→해시→이름 바꾸기로 굳히기, 게시는 굳힌 PNG·캡션만(판이 다르거나 유실되면 재렌더 없이 검토로), 기존 승인 원고 마이그레이션 | 제안 |
| [0055](0055-deck-comic-sticker-and-per-asset-ai-label.md) | 덱 JSON에 2컷 만화(comic: 컷 2개, 라벨 30자·말풍선 40자 HTML)·보조 캐릭터 스티커(ImageAsset)·장별 AI 표기(ai_slides: None=레거시, []=없음)·밈 템플릿. 사진 우선형·밈 경로에서 코드 도형 캐릭터 제거, 소재 사진 색. **동결 계약 변경, 합의 필요** | 제안 |
| [0056](0056-meme-path-and-scenario-writer.md) | 밈은 정보 원고와 분리된 경로: ScenarioWriter 포트(아이디어→대본), 호출별 밈 말투, 중복은 상황+반전 겹침이고 대비 구조도 같을 때만, 사실 표현은 검토 신호, 훅 있을 때만 claims. 포트 추가(합의 필요) | 제안 |
| [0057](0057-pick-slots-state-machine.md) | 추천 슬롯 표(날짜+종류+번호, 계획부터 존재): 정보 N+밈 M, 오늘만 변경, 축소 시 미시작분만 제외, claim·한 트랜잭션 저장, 실패→복귀 단계·next_attempt_at 재시도, 대체는 requested/actual 기록, 기존 picks 마이그레이션 | 제안 |
| [0053](0053-workspace-settings-and-run-snapshot.md) | 관심 분야 별칭·추천 구성·캐릭터·디자인은 작업 공간(프리셋)별 설정(preset.yaml에 저장), 키·공급자·모델·비용 상한은 전역. 어댑터는 한 묶음으로 만들고 잡은 시작 때 스냅샷을 받아 끝까지 쓴다. "없음"과 "빈 값"을 구분해 호환 | 제안 |
| [0054](0054-openai-single-key-and-cost-ledger.md) | OpenAI 키 하나로 글·답글·그림. 공급자 auto/openai/claude(명시하면 조용한 전환 없음). 공용 클라이언트는 재시도 없이 오류 분류만, 이미지는 전송 뒤 응답 없음을 unknown으로. 전역 비용 원장(예약→확정 같은 행, 미확인은 예약액). ImageStudio·CostLedger 포트 추가(동결 변경, 합의 필요) | 제안 |
| [0052](0052-deck-products-facts-slides.md) | 덱 JSON에 상품 그리드(products 2~4개: 이름·가격·보조·그림)·정보 패널(facts 2~6줄) 장과 가운데 장을 섞는 소식형(editorial) 템플릿을 더한다. 상품 이름·가격이 자료에 없으면 검토로. **동결 계약 변경, 합의 필요** | 제안 |
