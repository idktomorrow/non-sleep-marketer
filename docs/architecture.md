# 현재 구현 아키텍처

> 갱신: 2026-10-21 (S15 제안: 출연진·캐릭터 프로필·영속 그림 작업·승인 산출물, ADR 0058·0059. S14 제안: 덱 계약 2(2컷 만화·스티커·장별 AI 표기·밈 템플릿)·밈 경로(ScenarioWriter)·추천 슬롯 상태 기계, ADR 0055·0056·0057. S13 제안: 작업 공간별 설정·실행 스냅샷·OpenAI 단일 키 운영·전역 비용 원장, ADR 0053·0054. C29: 프리셋(작업 공간, DB 분리, ADR 0049). S12 제안: 덱 계약 확장 products·facts·editorial 템플릿, ADR 0052. S11 제안: 오늘의 추천·보관함·사진 후보, ADR 0051. S10 제안: 사진 우선형 카드 스타일 `editorial`, ADR 0050. C28: "오늘" 화면 비율·다음 게시 줄바꿈. C27: 분류기 Gemini 무료 구간·jev 제거(ADR 0048). C9: 인스타그램 요청 제한(429) 쉬기·댓글 확인 간격, ADR 0038. C10: 작업 요약을 사람이 읽는 문장으로. C11: 실행 기록은 작업 상태 표 + 변화가 있던 실행만, ADR 0040. C12: 안정화(계속 0건 알림, 데이터 백업, 실행 기록 정리, 스케줄러 종료 경합), ADR 0041. C13: 대시보드 간단·고급 모드, ADR 0042. C14: 카드 가상인물 배지는 표지·마지막 장에만, ADR 0043. C15: 키워드로 원고 만들기·수집 제외어, ADR 0044. C16: 올린 이미지를 읽어 자료로, ADR 0045. C17: 캐릭터·로고 그림, ADR 0046. C18: 댓글 답글이 원고에 쓴 자료까지 봄, ADR 0047. C19: 캐릭터 AI 표기 기본 끔. C20: 동봉 캐릭터 빼고 기본 "없음", 카드 캐릭터 260px. C21~C25: 인스타그램 화면 변경·답글 스레드·확인 안 된 답글·실패 상황 검증. C26: 버전 표시·다음 게시 시각). `v0.4-quality`(2026-10-15: S-β 계약 테스트, S-γ 품질 체크리스트·표지 썸네일 가독성, S8 카드 스타일 clean·photo·illust와 설정 화면 견본 선택까지 머지. 세 배치 모두 메인 담당(ADR 0034), 카드 스타일은 ADR 0036 채택, 원고별 스타일은 ADR 0037 제안으로 남음) · 약속은 [contracts.md](contracts.md), 이 문서는 "지금 실제로 어떻게 돌아가는가". 배치마다 맨 아래 구현 상태 표를 갱신한다.

## 1. 한 장 요약

사용자 PC에서 `nsm run` 하나가 세 가지를 동시에 띄운다: 스케줄러(잡 7개), 엔진(포트를 통해 외부 접근), 로컬 대시보드(FastAPI, 127.0.0.1:8765). DB는 DuckDB 파일 하나이고 이 프로세스만 연다.

```mermaid
flowchart LR
  subgraph P["nsm run (단일 프로세스)"]
    S[APScheduler<br/>잡 7개] --> E[엔진 잡<br/>run_once ctx]
    W[FastAPI 대시보드<br/>Jinja2 + HTMX] --> R[(DuckDB<br/>data.duckdb)]
    E --> R
    E --> PT[포트]
  end
  PT --> PA[플랫폼 어댑터<br/>instagram · threads · dryrun]
  PT --> SRC[수집 소스<br/>upload · naver · google_trends · community]
  PT --> CL[분류기<br/>gemini · claude · keyword]
  PT --> GEN[생성기<br/>claude_text · image_api]
  PT --> RD[렌더러<br/>카드 덱 → PNG]
  PT --> NT[알림<br/>Windows 토스트]
  PA --> DRV[브라우저 드라이버<br/>aside · playwright]
  DRV --> Web[(인스타그램 / 스레드 / 네이버 검색)]
  CL --> GM[(Gemini API 무료 구간)]
  GEN --> API[(Claude API / 이미지 API)]
  U[사용자 브라우저] --> W
```

## 2. 잡과 주기

| 잡 | 주기 | 입력 → 출력 |
|---|---|---|
| health | 30분 | 어댑터 `health()` → 로그아웃 시 알림, 플랫폼 중단 플래그. 요청 제한(429)은 멈추지 않고 쉬기만(ADR 0038) |
| collect | 하루 2회 ±30분 | 소스 → `source_items`(중복 제거) → 관련성·주제 `classifications` |
| generate | 하루 1회 | 주제 선정(14일 중복 제외) → `contents`(draft → pending_review / approved) |
| publish | 게시 슬롯 | approved 1건 → publishing → 렌더 → 어댑터 → published + `publications` |
| reply | 5분 ±1분(설정) | 새 댓글 → 6분류 → 답글 / 숨김 / 에스컬레이션, `reply_counts`. 답글을 보냈지만 어댑터가 확인 못 하면(빈 ID) 재시도 없이 에스컬레이션(C23). 보내기 전 'replying'으로 적고 켤 때 남은 'replying'은 에스컬레이션(`recover_interrupted_replies`, C24). 인스타그램은 30분마다·최근 3일 글만(ADR 0038). 답글 근거는 게시물 본문 + 그 원고에 쓴 자료 최대 3개(`repo.sources_for_content`, C18, ADR 0047) |
| metrics | 하루 1회 | 최근 7일 게시물·계정 지표 스냅샷 |
| weekly_report | 주 1회 | 성과 요약·추천 주제 → 대시보드 + md |
| maintenance | 매일 04:30 | 데이터 백업(`backups/data-날짜.duckdb`, 최근 7개, `COPY FROM DATABASE`) + 90일 넘은 실행 기록 정리(C12) |

## 3. 데이터 흐름

```mermaid
flowchart TD
  A[자료 수집] -->|SourceItem| B[관련성 ≥ 0.8 · 주제 분류]
  B --> C[주제 선정 · 생성<br/>덱 + 캡션 + 스레드 + 블로그 + claims]
  C --> D{정책 decide_publish}
  D -->|보호 모드 / 출처 누락 / 금칙어 / 신뢰도 < 0.8| Q[검토 대기열<br/>pending_review]
  D -->|통과| AP[approved]
  Q -->|대시보드 승인·수정| AP
  Q -->|폐기| X[discarded]
  AP --> PB[publishing → 렌더 → 어댑터 publish]
  PB -->|성공| PD[published + URL]
  PB -->|실패 ≤ 3회| RT[failed, 15분 · 1시간 · 4시간 후 재시도]
  RT -->|4회째| AB[abandoned → 검토 대기열]
  PD --> CM[댓글 수집 · 6분류 · 정책 decide_comment]
  CM --> RP[자동 답글 / 숨김 / 사람에게]
  PD --> MT[지표 스냅샷] --> WR[주간 리포트]
```

콘텐츠 상태: `draft → pending_review → approved → publishing → published | failed → abandoned`, 어디서든 `discarded`. 재시작 시 `publishing` 잔여 건은 `pending_review`로(이중 게시 방지).

## 4. DB 테이블 관계

```mermaid
erDiagram
  source_items ||--o{ classifications : "분류됨"
  source_items ||--o{ contents : "claims.source_item_id"
  contents ||--o{ publications : "플랫폼별 게시"
  publications ||--o{ comments : "post_external_id"
  publications ||--o{ metrics_posts : "스냅샷"
  comments }o--|| reply_counts : "author_id, date"
  contents ||--o{ costs : "ref"
  run_logs
  metrics_account
  settings_kv
```

## 5. 디렉터리 지도

[contracts.md 1절](contracts.md#1-디렉터리와-소유권)이 원본. 핵심만:
`nsm/domain`(모델·정책·상태 머신) → `nsm/ports`(Protocol) ← `nsm/adapters/*`(구현) · `nsm/engine`(잡) · `nsm/store`(DuckDB) · `nsm/app`(대시보드) · `nsm/scheduler` · `nsm/render` + `nsm/templates`(서브).

## 6. 구현 상태

범례: 계획 / 구현 중 / 완료 / 테스트됨(경계 1·2·3 중 어느 것). 배치 PR마다 갱신.

| 모듈 | 상태 | 테스트 | 배치 | 비고 |
|---|---|---|---|---|
| `nsm/cli.py` | 완료 | CLI 테스트 + 수동 | A·C·F·B·S8 | doctor(`--live` 실제 API 확인), init, run, run --once, render-preview(`--style`, 없으면 설정 스타일), install-autostart |
| `nsm/domain/models.py` | 완료 | 단위 | A | 동결. ADR 0009 반영 |
| `nsm/ports/` | 완료 | 단위(fakes 적합성) | A | 동결. `CostSink` 추가 |
| `nsm/store/` (스키마·리포지토리) | 완료 | 단위 | A(스키마), C | `Repository`: 중복 제거, 상태 전이 검사, 게시 재시도 조회, 답글 횟수, 비용 합계, 실행 로그 |
| `nsm/config/` (YAML·keyring·마법사) | 완료 | 단위 | C·S8 | 마법사는 `WizardIO`로 입출력 분리, 키는 환경 변수 → keyring 순. S8: `brand.card_style`(설정 쪽 `BrandSettings`, 도메인 `Brand`는 동결 유지), `image_generation` 기본 `off`, 마법사에서 OpenAI 키를 넣으면 켤지 확인(ADR 0036) |
| `nsm/adapters/sources/` 4종 | 완료 | 단위 + 실사이트 스모크 | D1·Q1 | 업로드·네이버(블로그·뉴스)·구글 트렌드 RSS·커뮤니티. Q1: 텍스트 정리, 관련 글 원문 본문 `fetch_body`(ADR 0022) |
| `nsm/adapters/classifier/` gemini·claude·keyword | 완료 | 단위(가짜 HTTP·클라이언트) + 운영 자료 294건 비교 | D1·C27 | Gemini 무료 구간(10건 묶음, 429 대기)이 기본, `FallbackClassifier`가 실패 시 Claude → 낱말 규칙으로 넘기며 알림. jev(MotherDuck)는 체험 종료로 제거(ADR 0048) |
| `nsm/adapters/generator/` 텍스트·이미지 | 완료 | 단위(가짜 클라이언트) | D2 | Claude(구조화 출력 + fallback), OpenAI 배경. 실호출은 키 확보 후 확인 필요. 네이버 블로그를 게시하지 않으면 블로그 원고 칸을 요청하지 않음(#43, ADR 0026) |
| `nsm/domain/policy.py` | 완료 | 단위 | D1·D2·E | 활동 시간·게시 시간(`should_post`, ADR 0027)·게시 판정·금칙어·배정·템플릿 순환·비용 상한·표기 삽입·댓글 판정 |
| `nsm/domain/labels.py` | 완료 | 단위 | A·B | 표기 문구. 배치 B에서 공정위 예규 원문으로 확정(ADR 0019) |
| `nsm/app/next_publish.py` + 버전 표시 | 완료 | 단위(`tests/unit/test_next_publish.py`) + 라우트(`tests/app/test_dashboard.py`) | C26 | 승인된 원고가 있을 때 플랫폼별 다음 게시 시각(대기/재시도 시각 → 요청 제한 → 같은 플랫폼 간격 → 하루 한도 → 게시 시간 `policy.next_window_start`). 바닥글 `app_version()`은 설치 정보에서 읽어 "지금 켜진 버전"을 보여 줌 |
| 대시보드 화면(`nsm/app/templates`, `static/app.css`, `ko.py`) | 완료 | 라우트(`tests/app`) + 화면 동작(`test_ui.py`) + 전후 스크린샷 | I | 오늘 할 일 중심 개요, 검토 상세 2단, 댓글 카드, 한국어 표시, Pretendard(`/fonts`), 브랜드 강조색, 휴대폰 폭(ADR 0025), 어절 단위 줄바꿈(`keep-all`)·개요 두 칸 1.25:1(C28), 설정 맨 위 프리셋 칸·상단 프리셋 배지(C29). 점검 도구 `scripts/dashboard_demo.py`·`dashboard_shots.py` |
| `nsm/app/run_view.py` + 실행 기록(`/logs`) | 완료 | 단위·라우트(`tests/app/test_run_view.py`) + 화면 캡처 | C11 | 작업 상태 표(`job_statuses`, 저장소 `latest_runs`·`last_success_at`), 아무 일 없던 성공 숨김·같은 결과 묶음(`build_rows`), `?all=1` 전체 보기. 오늘 화면 최근 실행도 같은 규칙(ADR 0040) |
| `nsm/render/character.py` + `nsm/app/brand_images.py` + 캐릭터·로고 그림 | 완료 | 렌더(`tests/render/test_character.py`) + 단위(`tests/unit/test_brand_images.py`) + 라우트(`tests/app/test_brand_image_routes.py`) + 브라우저 확인 | C17 | 설정 `brand.character`(none 기본·custom·drawn, C20) → `character_art()` → `CharacterArt`를 렌더러 옵션(`get_renderer(character=)`, 스타일처럼 렌더마다 설정에서 읽음)으로. 일러스트형 표지·마지막 장에 `<img class="mascot-img">`, `character_ai_made`(기본 끔, C19)면 그 장에 `ai-generated` 캡션. 렌더 캐시 지문에 그림 내용 포함(일러스트형만). 동봉 캐릭터 없음(C20에서 뺌). 올리기 `POST /settings/brand-image`(Pillow로 테두리에서 이어진 바탕만 지우고 자르기, `<NSM_HOME>/brand/`) (ADR 0046) |
| `nsm/adapters/sources/image_reader.py` + 이미지 자료 | 완료 | 단위(`tests/unit/test_image_reader.py`) + 라우트(`tests/app/test_materials.py`) + 실제 Claude 호출 2회 + 브라우저 확인 | C16 | 올린 png·jpg·webp(5MB 이하, 앞부분으로 형식 확인)를 Claude(`models.classifier`)가 `{has_info, text}`로 옮김. 결과는 `<NSM_HOME>/upload-text/` 캐시(경로·크기·수정 시각)라 올릴 때 한 번만 읽고 자료 모으기는 캐시를 씀. `UploadSource(read_image=)`, `save_material(read_image=)`, `AppState.read_image`(키 없으면 None → 이미지 거절) (ADR 0045) |
| `nsm/engine/keyword.py` + 키워드 원고(`/keyword-draft`) | 완료 | 엔진(`tests/engine/test_keyword.py`) + 라우트(`tests/app/test_keyword_draft.py`) + 브라우저 확인 | C15 | 키워드로 검색 소스(네이버·커뮤니티)만 바로 찾아 판정(키워드를 관심 주제로 더한 지시, 점수는 그 실행에서만 쓰고 DB에 남기지 않음) → 이번에 찾은 안 쓴 관련 자료 3건 → `_make_one`(평소 게시 규칙). 자료가 없으면 만들지 않음. 값 받는 잡 `ARG_JOBS`(`run_job(..., arg=)`, `JobScheduler.run_now(name, arg)`). 수집 제외어 `collect.exclude_words`: 수집 때 저장하지 않고 `_pick_material`에서도 뺌(ADR 0044) |
| 화면 모드(`/ui-mode`, 설정 `ui_mode`) | 완료 | 라우트(`tests/app/test_ui_mode.py`) + 브라우저 확인·캡처(`docs/samples/ui/modes/`) | C13 | 간단(기본)·고급 스위치. 간단은 자료·실행 기록 메뉴, "오늘"의 세부 칸·최근 실행, 설정 세부 값을 숨기고(폼에는 남아 같이 저장) "오늘"에 자료 올리기를 둠. 기능·라우트는 같음(ADR 0042) |
| `nsm/app/settings_form.py` + 설정 화면(`/settings`) | 완료 | 라우트·검사(`tests/app/test_settings.py`) + 예약 다시 걸기(`test_scheduler.py`) | M17 | 페르소나·브랜드·운영 값 편집, 저장 즉시 반영(`set_config`), 활동 시간이 바뀌면 `JobScheduler.reschedule`. 계정·드라이런·키는 제외(ADR 0028) S8: 카드 스타일을 견본 카드(지금 색·페르소나로 그린 표지·본문, `nsm/app/style_preview.py`, `/settings/style-preview/…`)를 보고 고름 |
| `nsm/app/card_editor.py` + 검토 상세 편집 라우트 | 완료 | 라우트(`tests/app/test_card_editor.py`) + 실측 | M16 | 슬라이드별 문구, 배경 올리기·지우기(형식·크기 검사, AI 표기 재계산), 미리보기 렌더(`media/previews`, 잡 스레드, 설정 스타일로). 원고별 스타일 선택은 2차(ADR 0037) |
| `nsm/domain/quality.py` | 완료 | 단위 | Q1 | 품질 규칙: 제외 장르, 카드 목록 번호 떼기, 이모지·딱딱한 표현 검사, 출처 줄(ADR 0023) |
| `nsm/engine/` 잡 8개 | 구현 중 | 경계 1 | D1(collect), D2(generate), E(publish), F(reply·health·metrics), H(weekly_report), C9 | 잡 7개 전부 완료, `runner`가 실행 로그·실패 알림, 재시작 복구. C9: 요청 제한(429) 쉬기(`rate_limit.py`, 쉬는 구간마다 알림 1번), 인스타그램 댓글 확인 간격·기간 따로(`tests/engine/test_rate_limit.py`, ADR 0038). C10: `summary.py`가 결과를 잡별 한국어 문장으로(`JobResult.summary()`, 지금 실행 알림 `ko.result_text`, 로그). DB의 예전 형식 줄은 화면 필터 `run_text`가 문장으로 바꿈(`tests/unit/test_summary.py`). C12: `maintenance` 잡(백업·실행 기록 정리), `watchdog.check_stalls`(health 잡이 30분마다: 계속 0건 → 오늘 할 일·하루 한 번 알림, `tests/engine/test_stability.py`) |
| `nsm/adapters/platforms/dryrun.py` | 완료 | 경계 2(드라이런) + E2E | E | 미리보기 저장, 게시 시각 기준 가짜 댓글·지표, 답글·숨김 로그 |
| `nsm/app/` 대시보드 | 구현 중 | 라우트 테스트 + 실서버 스모크 | F·H(백엔드), I(디자인) | 7개 화면(주간 리포트 포함)·CSRF·Host 검사·직접 답글. 화면은 최소 HTML(디자인은 I) |
| `nsm/scheduler/` + autostart | 완료 | 단위 + 실서버 스모크 | F | 작업 스레드 1개, 활동 시간 기준 트리거, 시작프로그램 폴더(ADR 0013) |
| `nsm/app/materials.py` + 자료 화면(`/materials`) | 완료 | 라우트·파일 검사(`tests/app/test_materials.py`) | M18 | 파일 올리기(형식·크기·읽기 확인, 안전한 이름), 올린 파일 상태, 모은 자료와 판정(ADR 0033) |
| `nsm/adapters/notify/recording.py` + 알림함·게시 대기 화면 | 완료 | 라우트(`tests/app/test_bell_queue.py`) | N2 | 모든 알림을 `notifications` 테이블(스키마 v3)에 남기고 데스크톱으로 넘김, 제목으로 이동 화면 결정. `/queue`: 승인된 원고, 검토로 되돌리기·폐기(ADR 0031) |
| `nsm/adapters/notify/` | 완료 | 단위(`test_notify.py`) + 실측 | F·N1 | Windows 토스트(앱 ID 등록, 이스케이프한 XML을 -EncodedCommand로) + 로그, 같은 제목 60초 내 중복 억제, 설정의 켜기/끄기(ADR 0030) |
| `nsm/render/` + `nsm/templates/` | 완료 | 경계 3(렌더) | S-α·S-γ·S8·S10 | `HtmlCardRenderer`(Jinja2 + Playwright, 전용 스레드), 템플릿 3종, Pretendard 동봉. Playwright 없을 때만 단색 대체. S-γ: 표지 글자를 스레드 썸네일(약 21%) 기준 최소 크기로, 제목 빈 줄 합침(ADR 0035) S8: 스타일 `clean`·`photo`·`illust`(`<body class="style-…">` + `photo.css`·`illust.css` + 코드로 그린 장식·SVG 캐릭터). 렌더러는 렌더할 때마다 설정 스타일을 읽고, 캐시 지문에 스타일 포함(ADR 0036) S10(제안): `editorial` 사진 우선형(`editorial.css` + `_parts.html`의 설명 패널·팔로우 카드, 사진이 있는 장에서만. 견본 사진 `nsm/templates/samples/`), ADR 0050 C14: 이름과 "가상인물" 배지는 표지·마지막 장에만, 중간 장은 둘 다 없음(ADR 0043) |
| `nsm/adapters/drivers/` aside·playwright | 구현 중 | 단위 + 스모크 | D1(수집용 Playwright), B, G1 | 기본값 `aside`(ADR 0017). `aside.py`: repl 실행기·일회용 파일 서버·수집용 `AsideDriver`(G1, ADR 0020). 수집은 Playwright 수집 전용 프로필 유지(ADR 0021) |
| `nsm/adapters/platforms/threads.py` | 완료 | 단위(가짜 실행기) + 실계정 계약(`-m contract`) | G1 | 게시(멱등)·댓글 읽기·답글·숨기기·지표·삭제, 계정 확인. `runtime.make_live_adapters`가 `settings.accounts` 있을 때만 생성 |
| `nsm/adapters/platforms/dryrun.py` | 완료 | 경계 2 + 단위 | D1·S-β | 드라이런 지표는 실계정과 같은 필드만 채움(스레드는 노출수·저장 없음) |
| `nsm/adapters/platforms/instagram.py` | 완료 | 단위(가짜 실행기) + 실계정 계약(`-m contract`) | G2·C9 | 게시(4:5 자르기, Threads 동시 공유 끔, 비율 확인, 멱등. C21: "알림 설정" 안내 창은 `nsmDismissPrompts`로 닫고 대화상자가 여럿이면 모두 찾음. C22: 답글은 "답글 달기" 뒤 입력창을 다시 클릭하지 않아야 댓글 아래에 달림. C25: 전송은 Enter(마우스 클릭은 초점을 빼앗아 답글 대상이 지워지는 경쟁이 있었음), 안 먹으면 "게시")·댓글 읽기·답글·지표·삭제. 숨기기 미지원(ADR 0018). C9: 공통 `aside_common`이 오류 화면을 로그인 풀림과 구분(`nsmCheckPage`), 429면 `RateLimitedError`와 2→4→8시간 쉬기(실제 429 화면으로 확인, ADR 0038) |
| `tests/fakes/`, `tests/conftest.py` | 완료 | - | A | 모든 포트의 가짜 + 픽스처 로더 |
| `tests/engine/` 시나리오 | 완료 | 경계 1 | D1·D2·E·F | 수집·생성·게시·대댓글·상태 점검·지표·드라이런 E2E |
| `tests/contracts/` | 완료 | 경계 2(어댑터 계약) | S-β, G | `test_platform_contract.py`: 드라이런(threads·instagram)으로 게시 → 주소, 댓글(since), 답글, 숨기기(capability 없으면 NotSupportedError), 지표 가용성 매트릭스, health. 실계정은 가짜 실행기로 같은 약속, `-m contract`로 읽기 전용 확인. 게시·삭제까지 하는 실계정 계약은 `test_threads_live.py`·`test_instagram_live.py`. 소유권은 S-β 동안 메인(ADR 0034) |
| `tests/render/` | 완료 | 경계 3 | S-α·S-γ·S8·S10 | PNG 크기·표기 슬롯·광고 배지·AI 캡션·극단 입력·스레드 충돌·표지 썸네일 가독성·제목 빈 줄(S-γ) S8: 스타일 × 템플릿 PNG·넘침, 표기 슬롯 동일, 코드 배경 AI 표기 없음, 글자 HTML·CTA 띠 없음, 세 스타일 썸네일 가독성, 밝은 브랜드 색 대비(`test_card_styles.py`) S10: 사진 있음/없음 × 보통/극단, 패널·팔로우 카드 조건, 패널 높이 35% 이하, 흰 패널 대비(`test_editorial_style.py`) |
| 덱 계약 확장(`nsm/domain/models.py` products·facts·editorial) + 생성기·편집 화면·렌더러 호출부 | 제안(합의 필요, 구현·테스트됨) | 계약(`tests/unit/test_contracts_frozen.py`·`test_editorial_deck.py`) + 경계 3(`tests/render/test_editorial_slides.py`) + 라우트(`tests/app/test_card_editor.py`) | S12 | `ProductsSlide`(2~4개, 그림은 로컬 경로)·`FactsSlide`(2~6줄), `editorial` 템플릿만 가운데 장 섞기. 생성기 스키마 `products`·`facts`, 사진 우선형 스타일일 때 소식형 후보, `unverified_products`로 이름·가격 자료 대조 → 검토. 상품 그림 `/review/{id}/slides/{i}/items/{j}/image` (ADR 0052) |
| `nsm/engine/picks.py` + `nsm/domain/picks_policy.py` + `nsm/store/picks.py` | 제안(구현·테스트됨) | 단위(`tests/unit/test_picks_policy.py`·`test_picks_store.py`·`test_og_image.py`·`test_naver_date.py`) + 경계 1(`tests/engine/test_picks_job.py`) | S11 | 오늘의 추천: 수집→사건 묶기(제목 토큰, 별칭·일본어 낱말 정규화, 날짜·고유명 분리)→`published_at` 기준 최신성→서로 다른 사건 최대 N개를 `build_draft`로 검토 대기 원고에. `picks` 표(스키마 v4)로 보관·사진 후보·렌더 확인·재확인. `count_contents_between`은 추천을 안 셈. og:image 후보 수집(`adapters/sources/og_image.py`, 사설 IP·크기·형식 차단) (ADR 0051) |
| `nsm/app/picks_routes.py` + `review_actions.py` + `templates/picks.html`·`_pick_card.html` | 제안(구현·테스트됨) | 라우트(`tests/app/test_picks_routes.py`) | S11 | `/picks`(오늘·보관함), 게시(승인+게시 레코드 한 트랜잭션)·보관·관심 없음(자료 해제+지문 보존)·재확인·사진 쓰기/안 쓰기. 기존 승인·폐기·게시 취소도 `ReviewActions`로 통일. 설정에 추천 모드·하루 추천 수·검색 별칭(관심 분야 묶음 버튼은 S16에서 제거) |
| `nsm/domain/production.py` + `nsm/config/characters.py` + `nsm/store/production.py` + `nsm/engine/production.py` + `images` 잡 | 제안(구현·테스트됨) | 단위(`tests/unit/test_production_domain.py`·`test_characters.py`) + 경계 1(`tests/engine/test_production.py`) | S15 | 출연진(주인공·보조, account/content/custom × generate/real_only, 원고 폴더 참조 복사본), 작업 공간별 캐릭터 프로필, 제작 슬롯(만화 컷·첫 메모 스티커), 슬롯별 spec 지문(대사 제외), 2컷 의존, `image_jobs`(queued→claimed→sent→결과, 보내기 전 재확인, 응답 불명 무재시도, 재시작 복구), `content_images` 이력·재사용·직접 올리기, 추천 슬롯 단계 파생. 스키마 v7 (ADR 0058) |
| `nsm/engine/artifacts.py` + `ApprovalGate`(`review_actions.py`) + `publish.render_images` | 제안(구현·테스트됨) | 라우트(`tests/app/test_production_routes.py`) + 경계 1(`tests/engine/test_publish_job.py`) | S15 | 미리보기 판(`preview_manifests`: 판 번호·지문·PNG 해시), 사람 승인은 그 판이 지금과 같을 때만 굳힘(임시→해시→이름 바꾸기, 캡션 포함), 아니면 새 미리보기 후 재승인. 게시는 굳힌 산출물만(판 다름·유실 → 재렌더 없이 검토로), 정책 승인은 첫 렌더를 굳힘, 기존 승인 원고 마이그레이션 (ADR 0059) |
| `nsm/app/production_routes.py` + 검토 상세 "그림"·설정 "캐릭터" | 제안(구현·테스트됨) | 라우트(`tests/app/test_production_routes.py`) | S15 | 출연진 바꾸기(저장된 캐릭터·이 콘텐츠의 캐릭터·이번만 쓸 참조), 그림 없는 자리 모두 만들기, 이 컷만 다시 만들기, 장면 지시 저장, 직접 올리기, 이전 그림 재사용, 다시 요청·과금 안 됨 확인. 캐릭터 만들기·참조 올리기/지우기·참조 그림 만들기(AI)·기본 출연진 |
| `nsm/domain/models.py` comic·sticker·ImageAsset·ai_slides·meme + 렌더러(`_parts.html` comic/sticker, `cards/meme.html`, `render/colors.py`) | 제안(합의 필요, 구현·테스트됨) | 계약(`tests/unit/test_contracts_frozen.py`·`tests/render/test_comic_slide.py`) + 경계 3(`test_comic_slide.py`·`test_sticker.py`·`test_editorial_style.py`) | S14 | 2컷 만화(라벨·말풍선 HTML, 그림 없는 컷은 자리 표시), 메모 상자 스티커, 장별 AI 표기(`ai_slides`, None=레거시·[]=없음), 밈 템플릿(첫 장 comic 가능). 사진 우선형·밈 경로에서 코드 캐릭터·소품 제거(`mascot.py` 삭제, illust는 유지), 소재 사진 색, 스티커 글자(`workspace.title_style`) (ADR 0055) |
| `nsm/engine/memes.py` + `nsm/domain/meme_policy.py` + 포트 `ScenarioWriter` + `generator/prompts.py` 밈 스키마 | 제안(포트 추가 합의 필요, 구현·테스트됨) | 단위(`tests/unit/test_meme_policy.py`) + 경계 1(`tests/engine/test_picks_slots.py`) | S14 | 아이디어 N개 → 중복 아닌 것(상황+반전 겹침 ∧ 대비 구조) → 2컷 대본 → 밈 덱(그림은 S15). 호출별 밈 말투(`workspace.meme_tone`), 훅 없으면 claims 없음, 사실 표현·정보 겹침은 검토 신호. Claude·OpenAI 생성기가 겸함 (ADR 0056) |
| `nsm/domain/topics.py` + `nsm/domain/meme_discovery.py` + `nsm/adapters/sources/meme_discovery.py` + `engine/memes.py`·`engine/picks.py` 주제 연결 | 제안(포트 필드 추가 합의 필요, 구현·테스트됨) | 단위(`tests/unit/test_topics_and_discovery.py`·`test_search_terms.py`) + 경계 1(`tests/engine/test_picks_slots.py`) + 라우트(`tests/app/test_picks_routes.py`) | S16 | 관심 주제 하나가 수집→후보→대본→그림 계획까지. 종류별 최근 이력 순 주제 순환(`topic_order`), 순위 묶음을 주제별로 섞기, 밈 사례 탐색(네이버 블로그·카페·커뮤니티 공개 검색, X는 미수집·링크만, 반응 수치 null), A 최근 확산/B 지속 형식/C 근거 부족/D 창작 분류, 의미 중복(`same_as_recent`), 주인공은 고른 캐릭터만(없으면 대본+후보 제안 후 참조 대기), 계정 설명-주제 충돌 경고, 추천 카드 근거 펼쳐보기. `interest_presets.py`·`/settings/interest-preset` 제거 (ADR 0060) |
| `nsm/adapters/sources/meme_formats.py` + `engine/memes.py` 형식 경로 + `MemeImageSlide`·`MemeOverlay` + 렌더러 `meme_image` + 포트 `plan_meme_format` | 제안(덱 계약·포트 추가 합의 필요, 구현·테스트됨) | 단위(`tests/unit/test_meme_format_parts.py`) + 경계 1(`tests/engine/test_meme_formats.py`) + 라우트(`tests/app/test_picks_routes.py`) | S17 | 밈 형식 탐색(Know Your Meme 인기·유명 형식, 캐릿 유행 중, 적힌 근거만, 같은 날 캐시) → 모델이 후보 점수·선택·재해석·글자 배치 → 한 장 덱(마무리 장 없음, 작은 출처 표기). 제작 슬롯 `s0.image`(1024×1280, 캐릭터 참조 + 밈 참조 마지막, 역할·글자 자리 지시). 말풍선 꼬리는 렌더 때 화자 쪽, 장 밖 글자는 넘침. 캐릭터 참조 출처(`reference_sources`)가 출연진·계획·캡션까지. `meme_remake` 작업과 "다른 밈으로 다시 만들기" (ADR 0061) |
| `engine/photo_match.py` + `og_image.fetch_article_images` + `ReviewActions.photo_use` + 덱 `photo`·`FactsSlide.layout`·`CardDeck.sources` + 렌더러(사진 칸·비율 맞춤 표지·정보 줄 배치·SVG 말풍선) + 밈 기획 세 축·`pages`·팬/창작 후보 + `nsm run --manual-only` | 제안(덱 계약 추가 합의 필요, 구현·테스트됨) | 단위(`tests/unit/test_content_led_design.py`·`test_meme_format_parts.py`) + 렌더(`tests/render/test_html_renderer.py`) + 라우트(`tests/app/test_picks_routes.py`) + 경계 1(`tests/engine/test_meme_formats.py`) | S18 | 소식형은 장마다 내용에 맞는 표현, 정보 줄 배치는 데이터 검증, 마무리 장 선택. 기사 사진 여러 장(대표·본문, 설명 문구, 평균 해시 중복 제거)을 그림을 보는 모델이 본문 장 먼저·표지 나중으로 내용이 맞는 곳에만 배치. 사진은 원본 비율(표지는 비율이 많이 다르면 흐린 배경 위), 사진 출처 표기, 사진 없음 경고. 밈은 소재·웃음 방식·형식, 팬 사례·창작 후보, 페이지 날짜로 최근 판정, 여러 장(2장째는 1장째 참조). 말풍선 몸통+꼬리 경로 하나 (ADR 0062) |
| `nsm/store/slots.py` `SlotStore` + `pick_slots`(스키마 v5) + `engine/picks.py` 재구성 + `slots_retry` 잡 | 제안(구현·테스트됨) | 단위(`tests/unit/test_slot_store.py`) + 경계 1(`tests/engine/test_picks_slots.py`·`test_picks_job.py`) + 라우트(`tests/app/test_picks_routes.py`) | S14 | 정보 N+밈 M 계획(오늘만 변경은 프리셋 DB `settings_kv`), claim·한 트랜잭션 저장, 축소 시 완성분 유지, 실패→복귀 단계·`next_attempt_at`(15분 잡), 대체 종류 기록, v4→v5 마이그레이션, 재시작 복구. 추천함에 구성 폼·슬롯 표·재시도, 그림 미완성 밈은 게시 관문 409 (ADR 0057) |
| `nsm/config/schema.py` `WorkspaceSettings` + `presets.py` | 제안(구현·테스트됨) | 단위(`tests/unit/test_workspace_settings.py`) | S13 | 별칭·정보/밈 추천 수·대체 허용·제작 모드·기본 캐릭터·디자인 옵션은 프리셋(작업 공간) 파일에 함께 저장·복원. 예전 `picks.count/aliases`는 workspace 절이 **없을 때만** 가져옴(빈 값은 그대로). 키·공급자·모델·비용 상한은 전역 (ADR 0053) |
| `nsm/engine/context.py` `Adapters`·`snapshot()` + `runtime.refresh_adapters` | 제안(구현·테스트됨) | 단위(`tests/unit/test_provider_selection.py`) | S13 | 어댑터는 한 묶음으로 만들고 잡은 시작 때 스냅샷을 받아 끝까지 그것만 쓴다. 키 등록·공급자/모델 설정 저장·프리셋 전환 뒤 묶음을 새로 만들어 다음 잡부터(재시작 없음) (ADR 0053) |
| `nsm/adapters/openai_common.py` + `generator/openai_text.py` + `generator/prompts.py` | 제안(구현·테스트됨) | 단위(MockTransport, `tests/unit/test_openai_text.py`) | S13 | OpenAI Responses API strict JSON 스키마로 원고·답글. 프롬프트·스키마·덱 조립은 Claude와 공용(`prompts.py`). 공용 클라이언트는 재시도 없이 오류를 분류만(전송 전/후, auth·quota·rate_limit·overloaded·moderation). 텍스트는 전송 뒤 응답 없음일 때 1회만 재시도. `text_provider` auto/openai/claude (ADR 0054) |
| `nsm/adapters/generator/image_api.py` `OpenAIImageStudio` + 포트 `ImageStudio` | 제안(구현·테스트됨, 포트 추가 = 동결 변경 합의 필요) | 단위(`tests/unit/test_openai_image_studio.py`) | S13 | 참조 이미지(≤16, `/images/edits`)·투명 배경·크기 규칙·품질. 정책 차단 `ImageRefused`, 전송 뒤 응답 없음 `ImageUnknown`(자동 재시도 금지), 한도 `ImageTransient`(재예약), 키·크레딧 `ImageUnavailable`. S15의 스티커·만화 컷이 쓴다 (ADR 0054) |
| `nsm/store/costs.py` `CostLedger` + 포트 `CostLedger` | 제안(구현·테스트됨) | 단위(`tests/unit/test_cost_ledger.py`) | S13 | 전역 비용 원장(`<NSM_HOME>/costs.duckdb`): 작업 공간을 바꿔도 하루 상한은 하나. 예약→확정은 같은 행(이중 합산 없음), 전송 전 실패는 해제, 전송 뒤 응답 없음은 미확인(예약액을 계속 셈). 이미지 비용은 이미지/텍스트 입력·출력 토큰 단가를 따로 곱하고 usage 없으면 추정 표시. 작업 공간 DB `costs`는 표시용 사본 (ADR 0054) |
| `nsm/app/keys_routes.py` + 설정 "AI 공급자·모델"·"API 키" | 제안(구현·테스트됨) | 라우트(`tests/app/test_keys_routes.py`) | S13 | 키 등록 여부만 표시, 등록·삭제는 keyring에만(값은 응답·로그에 없음), 등록 즉시 어댑터 갱신. 글 공급자·OpenAI 모델·이미지 모델·품질 선택 |
| `tests/app/` | 완료 | 라우트 | F | 승인·폐기·수정·토글·댓글 답글/완료·CSRF·경로 조작 차단 |

## 7. 설계와 구현의 차이

배치 중 계약과 다르게 구현했거나 미뤄 둔 것을 여기 적는다(없으면 "없음").

- 콘텐츠 상태와 플랫폼별 게시 상태를 분리했다(ADR 0009). 계약 초안의 `publishing/failed/abandoned`는 `publications.status`로 옮김.
- 브라우저 드라이버 기본값을 Playwright로 둠(ADR 0010).
- 카드 렌더는 `HtmlCardRenderer`가 한다. `PlaceholderRenderer`(단색 PNG)는 Playwright가 없는 환경의 대체로만 남았다. 렌더는 수집 드라이버와 Playwright가 충돌하지 않도록 전용 스레드에서 한다.
- 분류기는 용도별 묶음(`ClassifierSet`: 관련성·주제·댓글)으로 조립한다. 키워드 대체 구현 때문(ADR 0011).
- 자료의 관련성은 "relevant일 확률"로 통일해 `source_items.relevance`에 저장한다.
- collect 잡은 배치 E가 아니라 D1에서 먼저 만들었다(수집 결과를 실제로 확인하기 위해).
- generate 잡과 게시 판정 정책도 D2에서 먼저 만들었다.
- "최근 14일 주제 제외" 대신 페르소나 주제 순환 + 자료 1회 사용으로 바꿨다(ADR 0012). `topic_cooldown_days`는 아직 안 쓴다.
- DB 스키마 v2: `contents.persona_topic`.
- 같은 플랫폼 게시 사이 최소 간격(`min_hours_between_posts`, 기본 3시간)을 추가했다. 계약 초안의 "활동 시간 내 무작위 시각"은 F의 스케줄러 지터 + 이 간격으로 구현한다.
- 재시작 복구(`recover_interrupted`)는 프로세스가 DB를 열 때마다 실행한다. DuckDB 잠금 때문에 다른 프로세스가 동시에 게시 중일 수 없어서 안전하다.
- 스케줄러는 작업 스레드 1개로 모든 잡을 차례로 돌린다(Playwright 동기 API 스레드 제약). 대시보드의 플랫폼 동작도 `submit()`으로 같은 스레드에서 실행한다.
- 자동 실행은 작업 스케줄러 대신 시작프로그램 폴더(ADR 0013).
- 대시보드 POST는 CSRF 토큰을 검사한다(127.0.0.1 전용이어도 다른 웹사이트가 브라우저를 통해 요청을 보낼 수 있음).
- 댓글 분류 신뢰도 0.6 미만이면 사람에게 넘긴다(계약에 없던 안전장치).
- 주간 리포트는 LLM 없이 규칙으로 계산하고, 기간은 현지 날짜 기준 지난 7일 전체로 고정한다(ADR 0014).
