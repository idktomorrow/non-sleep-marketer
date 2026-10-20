# 관심 분야별 사진 중심 추천 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 구현 담당은 Claude이며 현재 문서는 검토용 계획이다. 이 문서 작성은 구현·게시·커밋 실행 요청이 아니다.

**Goal:** 관심 분야의 최신 사건을 선정하고 정확한 원본 이미지로 완성한 게시물 최대 3개를 매일 추천하며, 보관·수정·게시할 수 있게 한다.

**Architecture:** 기존 엔진 앞에 이슈 발견·순위·이미지 검증 단계를 추가한다. 신규 추천 저장소와 VisualManifest를 기존 콘텐츠·게시 상태에 연결하며 기존 게시 어댑터와 안전장치를 재사용한다. 기존 모드는 유지하고 신규 추천 모드는 설정으로 선택한다.

**Tech Stack:** Python 3.12, Pydantic, DuckDB, FastAPI, Jinja2, HTMX, APScheduler, httpx, Pillow, Playwright, 기존 AI 클라이언트.

**Spec:** `docs/superpowers/specs/2026-10-20-daily-editorial-design.md`를 먼저 읽는다. 이 문서의 수치·선택은 사용자 검토용 제안이며 구현 착수 전 받은 수정사항을 두 문서에 같이 반영한다.

## Global Constraints

- `CLAUDE.md`, `docs/contracts.md`, `docs/process.md`를 먼저 읽고 소유권·배치 DoD를 적용한다. 모델·포트·덱 JSON 동결 계약 변경은 이번 기본안에 포함하지 않는다.
- 기존 Python/FastAPI/Jinja/HTMX/DuckDB/APScheduler를 유지한다. 신규 엔진·저장소·라우트는 파일을 나눠 추가한다.
- 기본 1080×1350, 4~8장. 내용이 적으면 3장까지 허용하고 10장 계약 한도를 지킨다.
- 추천 수 기본 3개와 게시 수 별도 관리. 부족하면 적게 제공한다. 새로운 모드의 기본 게시 방법은 사용자 선택이다.
- 추천 원고가 기존 자동 승인·플랫폼 배정·하루 생성 수에 섞이지 않게 한다.
- 시크릿은 기존 keyring 정책을 따른다. 외부의 사실·사진을 생성형 이미지로 대체하지 않는다.
- 실계정 테스트는 기본 실행에서 제외한다. UI·렌더 검증은 격리된 임시 `NSM_HOME`과 드라이런으로 수행한다.
- render/templates 소유권은 기존 규칙대로 해당 범위 PR로 처리한다. 기존 전역 photo 스타일 변경으로 모든 저장 원고가 바뀌지 않게 신규 manifest 경로를 사용한다.

## Review Focus

- 같은 사건을 다룬 여러 기사·재시도·날짜 변경이 중복 추천을 만드는가: Task 2, 5, 7.
- 인기도·작성일·이미지 사용 근거가 없는 후보를 자동 게시 가능한 것으로 오인하는가: Task 2, 3, 6.
- 보관한 글의 가격·행사 날짜 변경 및 원문 삭제가 게시 시 반영되는가: Task 6.
- 추천 생성이 기존 승인·출처 사용 표시·하루 쿼터를 잘못 소모하는가: Task 5, 6.
- 이미지 다운로드·렌더 실패 또는 종료 중단으로 절반 생성된 항목이 게시되는가: Task 3, 4, 7.

## 파일과 데이터 경계

신규 모델은 `nsm/domain/editorial.py`, 순수 선정 함수는 `nsm/domain/editorial_policy.py`, 새 저장소는 `nsm/store/editorial_repo.py`에 둔다. 기존 Repository에 추천 기능 전체를 넣지 않는다.

새 테이블은 `nsm/store/schema.sql`과 `db.py` 마이그레이션으로 추가한다. 최소 관계:

- `editorial_events`: profile_id, event_id, event_version, entity IDs, event_type, event_date, facts와 source IDs, fingerprint. 같은 사건 버전은 unique.
- `editorial_observations`: event/source ID, metric, value, observed_at, provenance. 관측값 없는 지표는 NULL.
- `editorial_assets`: asset_id, source_id, URLs, local_path, hash, dimensions, match_status, usage_status, checked_at, evidence.
- `editorial_runs`: profile/date, run status, config snapshot, stage checkpoints. profile/date는 unique.
- `editorial_recommendations`: recommendation_id, run_id, event_id/version, state, score breakdown, reason, content_id, current_revision, saved_at, origin_content_id. profile/event/version은 unique. 다음날 다시 보일 때 새 원고 대신 기존 항목 참조.
- `editorial_content_sources`: content_id, source_id. 출처별 단일 사용 표시와 독립적인 다대다 연결.
- `editorial_visuals`: content_id, revision, versioned manifest JSON, checked_at. 참조 파일은 보관 시 유지.

`EditorialProfile` 설정 필드: id, name, category, entities, aliases, languages, region, include_words, exclude_words, source_rules. `EditorialSettings`: enabled=False(기존 사용자 보존), recommendation_count=3, publish_mode='manual', auto_select_count=1, fresh_hours=72, fallback_days=7. 설정 화면 저장 시 숨겨진 기존 값도 보존한다.

신규 주요 DTO:

- `EventCandidate`: event ID/version, profile ID, source IDs, entity IDs, type, facts와 근거, published_at/event dates, relevance, interest observations, asset IDs.
- `RankedCandidate`: candidate, total score, component scores, reason, freshness label, blockers.
- `MediaAsset`: 출처·파일 메타데이터 및 대상 일치/사용 상태. 상태 enum은 `pending/verified/rejected`, 사용 상태는 `unknown/allowed/blocked`.
- `VisualManifest`: schema_version, content_id, revision, slides. 각 slide는 slide_index, layout(`hero/photo_caption/product_grid/fact_panel/outro`), asset IDs, crop focal points, title_position, 출처가 연결된 product labels.
- `Recommendation`: 추천 테이블에 대응. state enum은 spec과 동일.
- `PublishCheck`: ok, reasons, checked_at, source_hashes. 변경 탐지 시 ok=False.

## Task 1: 설정과 추천 저장소

**파일:** 새로 만들기 `nsm/domain/editorial.py`, `nsm/store/editorial_repo.py`; 수정 `nsm/config/schema.py`, `nsm/store/schema.sql`, `nsm/store/db.py`, `nsm/config/store.py`; 테스트 `tests/unit/test_editorial_repo.py`, `tests/unit/test_editorial_config.py`.

**인터페이스:** `EditorialRepository(db: Database)`, `save_event(event: EventCandidate) -> str`, `save_recommendation(rec: Recommendation) -> None`, `get_recommendation(rec_id: str) -> Recommendation | None`, `transition(rec_id: str, expected: str, target: str) -> bool`.

- [ ] 기존 설정의 읽기·저장, 기존 DB 마이그레이션, 재적용, 동일 사건 중복 삽입 거절, 상태 경쟁 거절, 재시작 후 보관 유지 테스트를 작성한다.
- [ ] `uv run pytest tests/unit/test_editorial_repo.py tests/unit/test_editorial_config.py`에서 미구현으로 실패하는지 확인한다.
- [ ] 앞 절의 DTO·테이블·설정을 구현한다. 상태 변경은 조건부 UPDATE로 처리하고 콘텐츠 출처는 다대다 관계로 저장한다. 기존 동결 모델은 변경하지 않는다.
- [ ] 같은 명령으로 테스트를 통과시킨다. 새 설정이 없는 기존 사용자는 기존 모드로 동작해야 한다.
- [ ] 승인된 작업 브랜치에서 `feat(editorial): add profiles and recommendation storage`로 커밋한다.

## Task 2: 분야별 수집과 사건 단위 선정

**파일:** 새로 만들기 `nsm/adapters/sources/editorial.py`, `nsm/adapters/classifier/events.py`, `nsm/domain/editorial_policy.py`, `nsm/engine/discover.py`, `tests/fixtures/editorial/`; 수정 `nsm/adapters/sources/web.py`, `nsm/engine/collect.py`; 테스트 `tests/unit/test_editorial_ranking.py`, `tests/engine/test_discover.py`.

**인터페이스:** `discover_candidates(ctx: JobContext, profile: EditorialProfile) -> list[EventCandidate]`, `rank_candidates(candidates: list[EventCandidate], now: datetime, limit: int = 3) -> list[RankedCandidate]`. Task 1 모델·저장소에 의존한다. 시간은 Clock에서 주입한다.

- [ ] 같은 발표의 다른 URL은 한 사건, 같은 캐릭터의 다른 상품은 다른 사건, 전재 기사는 독립 보도 수에 미포함이라는 테스트를 쓴다. 작성일 미상, 과거 기사 재수집도 포함한다.
- [ ] `uv run pytest tests/unit/test_editorial_ranking.py tests/engine/test_discover.py` 실패를 확인한다.
- [ ] 네 분야 검색 프리셋을 만든다. 실제 공식 출처를 조사하고 URL·확인일·수집 방식·얻은 필드를 `docs/editorial-source-registry.md`에 기록한다. 분야별 실취득 샘플 1개를 확인하고 미확인 출처는 비활성화한다.
- [ ] 공개일·수정일·행사일을 구분한다. 공식 발표는 소재로 받고 광고 문구·체험담·소문은 무조건 채택하지 않는다. 기존 브라우저 직렬 실행 제약을 유지한다.
- [ ] 근거 문장을 연결한 구조 추출과 사건 병합을 구현한다. 순위 함수는 spec의 가중치, 72시간→7일 확장, 향후 행사 예외, 14일 중복 억제, 같은 유형 최대 2개를 적용한다.
- [ ] 관측 없음이면 급상승 표시 금지, 비교 가능한 지표 관측이 있으면 표시 가능, 키워드 대체 분류만으로 확인 완료 금지 테스트를 추가해 모두 통과시킨다.
- [ ] `feat(editorial): discover and rank distinct current events`로 커밋한다.

## Task 3: 원본 이미지 확보와 검증

**파일:** 새로 만들기 `nsm/adapters/sources/media.py`, `nsm/adapters/classifier/media_match.py`, `nsm/engine/media_assets.py`; 수정 `nsm/config/paths.py`; 테스트 `tests/unit/test_editorial_media.py`.

**인터페이스:** `prepare_assets(ctx: JobContext, event: EventCandidate) -> list[MediaAsset]`. Task 2 자료에서 후보 이미지를 얻고 Task 1 저장소에 결과를 저장한다.

- [ ] 다른 상품·지난 행사·로고만 있는 이미지·낮은 해상도·사용 상태 unknown·정확한 상품 이미지 테스트를 쓴다.
- [ ] `uv run pytest tests/unit/test_editorial_media.py` 실패를 확인한다.
- [ ] 다운로드 상한 10MB, 20초, 최대 25MP, http(s) 공개 IP만 허용, 리다이렉트 매 단계 검사, 파일 형식과 Pillow 검사를 구현한다.
- [ ] 파일 해시로 중복을 제거한다. 대상 일치와 사용 가능 근거는 별도로 기록한다. match=verified 및 usage=allowed일 때만 자동 채택한다. 이미지 생성으로 대체하지 않는다.
- [ ] 초기 사용 상태는 unknown. source_rules에 허용 범위·근거를 저장한다. 공식 도메인이라는 이유만으로 allowed로 바꾸지 않는다. 첫 운영에서 쓸 이미지 확보 가능성을 이 단계에서 검증한다.
- [ ] 내부 주소 리다이렉트, 과대·손상 파일, 연결 중단, 같은 이미지 다른 URL 테스트를 통과시킨다. 해당 후보만 실패하고 전체 작업은 계속되어야 한다.
- [ ] `feat(editorial): collect verified source images`로 커밋한다.

## Task 4: 사진 중심 편집과 렌더링

**파일:** 새로 만들기 `nsm/render/editorial_renderer.py`, `nsm/templates/cards/editorial.html`, `nsm/templates/cards/editorial.css`; 수정 `nsm/app/card_editor.py`, `nsm/engine/publish.py`; 테스트 `tests/render/test_editorial_renderer.py`, `tests/fixtures/editorial/visuals/`.

**인터페이스:** `render_editorial(deck: CardDeck, manifest: VisualManifest, assets: dict[str, MediaAsset], output_dir: Path) -> list[Path]`. Task 1/3의 DTO를 받는다. 기존 `render_images`는 manifest 유무로 새 렌더러와 기존 렌더러를 선택한다.

- [ ] 표지·사진 설명·제품 2~4개 그리드·정보 패널·마무리 fixture를 만든다. 누락 asset, 긴 상품명, 한일 혼합 문구, 제목 줄바꿈을 테스트한다.
- [ ] `uv run pytest tests/render/test_editorial_renderer.py` 실패를 확인한다.
- [ ] 표지 사진 70% 이상, 제목 2줄과 spec의 규칙을 구현한다. 이미지 초점·글자 위치·제품별 라벨은 manifest에서 읽고 모든 라벨을 품질 검사에 포함한다.
- [ ] 캐시 키에 deck, 전체 manifest, 이미지 해시, 렌더러·템플릿 버전을 포함한다. 누락·overflow·렌더 실패는 완성으로 처리하지 않는다.
- [ ] 실제 1080×1350 PNG와 270×338 축소본을 열어 상품·얼굴·글자 겹침을 확인한다. 기존 렌더 테스트도 통과시키고 기존 원고 모양을 보존한다.
- [ ] 소유권 규칙에 맞는 PR 단위로 `feat(render): add source-image editorial layouts`를 커밋한다.

## Task 5: 완성 원고 생성

**파일:** 새로 만들기 `nsm/engine/editorial_generate.py`; 수정 `nsm/engine/generate.py`, `nsm/adapters/generator/claude_text.py`, `nsm/store/repo.py`, `nsm/engine/context.py`; 테스트 `tests/engine/test_editorial_generate.py`.

**인터페이스:** `generate_recommendation(ctx: JobContext, candidate: RankedCandidate) -> Recommendation`. Task 2/3 통과 후보를 받아 deck과 Task 4 manifest를 같은 revision으로 저장한다.

- [ ] 다른 사건 기사 혼합 금지, 추천 3개가 기존 게시 쿼터를 소모하지 않음, 기존 자동 게시 ON이어도 새 수동 모드는 pending_review라는 테스트를 쓴다.
- [ ] `uv run pytest tests/engine/test_editorial_generate.py` 실패를 확인한다.
- [ ] `_make_one`의 생성·품질 검사 중 필요한 부분만 공통화한다. 저장·자동 승인·플랫폼 배정은 추천용으로 분리하고 기존 동작은 회귀 테스트로 지킨다.
- [ ] 사건의 출처만 brief에 넣는다. 짧은 제목·개요·제품 설명·일정·CTA를 만들고 사진 제품 라벨의 이름·가격이 출처와 일치하지 않으면 완성을 막는다.
- [ ] 추천 원고는 기존 `count_contents_between` 생성 목표 계산에서 제외한다. 추천 때문에 `used_in_content_id`를 소모하지 않고 다대다 출처 관계를 저장한다. `sources_for_content`는 기존·새 출처를 중복 없이 반환한다.
- [ ] 후보 예약·생성 완료·실패를 영속화한다. 비용 상한, API 중단, 2번째 후보 생성 중단, 완성본 재사용 테스트를 통과시킨다.
- [ ] `feat(editorial): generate complete daily recommendations`로 커밋한다.

## Task 6: 추천 화면과 보관과 게시 전 재확인

**파일:** 새로 만들기 `nsm/app/routes/editorial.py`, `nsm/app/templates/editorial.html`, `nsm/app/templates/saved.html`, `nsm/engine/editorial_publish_check.py`; 수정 `nsm/app/server.py`, `nsm/app/templates/overview.html`, `nsm/app/templates/base.html`, `nsm/app/settings_form.py`, `nsm/app/templates/settings.html`, `nsm/app/templates/review_detail.html`, `nsm/engine/publish.py`; 테스트 `tests/app/test_editorial.py`, `tests/engine/test_editorial_publish_check.py`.

**인터페이스:** `create_editorial_router(state: AppState) -> APIRouter`, `validate_for_publish(ctx: JobContext, content_id: str) -> PublishCheck`. 새 저장소 등 필요한 의존성을 AppState로 주입한다. 원문 재확인 같은 긴 작업은 기존 submit 작업 스레드에 넣는다.

- [ ] GET `/recommendations`, `/saved`, POST `/recommendations/{id}/save`, `/queue`, `/dismiss`, `/clone`의 CSRF·중복 클릭·404·409 테스트를 쓴다.
- [ ] `uv run pytest tests/app/test_editorial.py tests/engine/test_editorial_publish_check.py` 실패를 확인한다.
- [ ] 분야·대상·별칭 설정 UI를 구현한다. 기존 persona.topics를 새 profile의 초기값으로 제시하되 기존 설정을 몰래 지우거나 덮어쓰지 않는다.
- [ ] 오늘 추천, 부족한 이유, 이미지 확인 대기 후보, 보관 목록, 모든 장·캡션 미리보기, 사진 교체·크롭·문자 위치 편집을 연결한다. 편집하면 revision을 올리고 승인을 해제한다.
- [ ] 설정의 이미지 출처 관리와 후보별 이미지 확인·교체를 구현한다. 근거 없이 일괄 허용하지 않고, 사진이 없는 환경에서도 후보는 보인다. 관련 라우트 테스트를 추가한다.
- [ ] 큐 투입·승인·플랫폼 배정을 한 DB 트랜잭션으로 처리한다. 보관은 pending_review 유지, 게시 완료본 복제는 새 ID와 origin_content_id를 생성한다.
- [ ] 큐 진입과 실제 게시 직전에 검사한다. 확인 후 24시간 경과 또는 날짜·가격이 있으면 원문을 재확인한다. 변경·행사 종료·접근 실패는 검토로 돌리고 자동 수정·승인하지 않는다.
- [ ] 두 플랫폼 중 하나만 게시된 경우 나머지만 처리한다. 뉴스 노후화, 게시본 복제 경고, 저장 후 재시작, 설정 변경 후 기존 원고 보존 테스트를 통과시킨다.
- [ ] `feat(app): add recommendation inbox and saved drafts`로 커밋한다.

## Task 7: 일일 실행과 복구와 선택적 자동 게시

**파일:** 새로 만들기 `nsm/engine/editorial.py`; 수정 `nsm/runtime.py`, `nsm/engine/runner.py`, `nsm/scheduler/app.py`, `nsm/engine/summary.py`, `nsm/engine/maintenance.py`; 테스트 `tests/engine/test_editorial_pipeline.py`, `tests/unit/test_scheduler.py`.

**인터페이스:** `run_daily_editorial(ctx: JobContext) -> JobResult`. 수집·발견 → 예비 순위 → 이미지 준비 → 최종 재선정 → 생성 → 렌더 완료를 순서대로 실행한다. Task 1 저장소, Task 5 생성, Task 6 게시 검사를 사용한다. 후보 10개 또는 비용 상한까지 진행하고 준비된 최대 3개만 완성한다.

- [ ] 두 번 실행, 중간 종료 복귀, 설정 변경, 비용 상한, 3개 미만, 원문 시간 초과, 구 모드와 동시 예약 금지 테스트를 쓴다.
- [ ] `uv run pytest tests/engine/test_editorial_pipeline.py tests/unit/test_scheduler.py` 실패를 확인한다.
- [ ] 새 모드에서는 일일 generate를 daily_editorial로 대체한다. 기존 수집 예약과 겹친 자료는 중복 제거한다. 활동 시간과 실행 시작 설정 snapshot, profile/date 키를 유지한다.
- [ ] 이미지 검증 후 실제 준비도 점수로 다시 순위를 매긴다. 이미지만으로 날짜·근거 필수 조건을 상쇄하지 않는다.
- [ ] 재시작 시 preparing은 중간 결과부터 재개하고 publication 복구는 기존 경로를 쓴다. 저장된 원고는 재사용한다. API 응답 직후 저장 전 중단이면 해당 호출 재과금 가능성을 기록한다.
- [ ] 자동 게시 옵션도 이번 범위에 구현한다. 기본 manual은 기존 토글만으로 게시되지 않는다. auto일 때 기존 토글·protect_count·검증을 충족하는 최고 순위 최대 1건을 선택하고 플랫폼별 쿼터를 적용한다.
- [ ] DB·manifest·이미지 백업을 함께 복원한 뒤 모든 장을 렌더할 수 있는지 테스트한다. 보관 이미지가 캐시 청소로 삭제되지 않게 한다.
- [ ] `feat(engine): schedule and recover daily editorial runs`로 커밋한다.

## Task 8: 통합 검증과 전달

**파일:** 새로 만들기 `tests/engine/test_editorial_e2e.py`, `docs/editorial-acceptance.md`; 수정 `docs/user-guide.md`, `docs/architecture.md`, `docs/known-gaps.md`, `CHANGELOG.md`. 배치별 devlog·ADR·버전 변경은 기존 DoD를 따른다.

- [ ] 네 분야 fixture로 후보→자료→사진→완성→보관→재시작→게시 검증→dryrun까지 확인한다. 각 단계 이상 상황 테스트를 재사용한다.
- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`를 모두 통과시킨다. 계획 작성 전 기존 베이스라인은 773 passed / 13 deselected다.
- [ ] 격리 데모 환경의 desktop/mobile 추천·보관·편집 화면을 연다. 실제 PNG의 레이아웃·이미지 정확성·문자 잘림·조작을 확인하고 기록한다.
- [ ] 치이카와 실제 자료는 dryrun으로 검증한다. 출처와 사진의 일치, 서로 다른 사건, 0~3건인 이유를 사람이 확인한다. 적합한 실제 이미지가 확보되지 않으면 실데이터 검증 완료로 보고하지 않는다.
- [ ] 기존 DoD에 따라 문서·ADR·버전을 업데이트하고 리뷰를 받는다. API 키나 민감한 운영 자료를 fixture·로그·PR에 넣지 않는다.

## Claude에게 전달할 구현 요청

이 계획과 연결된 설계서, CLAUDE.md, docs/contracts.md, docs/process.md를 읽고 사용자가 확정한 수정을 반영한 뒤 Task 1부터 구현해 주세요. 기존 자동 게시와 동결 계약을 지키고 추천 수와 게시 수, 보관과 승인, 관련성과 유행도를 구분해 주세요. photo 스타일 변경만으로 끝내지 말고 실제 출처 수집, 정확한 원본 이미지, 보관 원고의 게시 전 재검증까지 구현해 주세요. 구현 내용, 실행한 테스트, 실제 PNG 확인 결과, 남은 제약을 보고해 주세요.
