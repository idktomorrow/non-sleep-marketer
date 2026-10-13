# 현재 구현 아키텍처

> 갱신: 2026-10-13 (배치 G 완료: 스레드·인스타그램 실계정. 남은 메인 배치: I 디자인, M16 카드 편집) · 약속은 [contracts.md](contracts.md), 이 문서는 "지금 실제로 어떻게 돌아가는가". 배치마다 맨 아래 구현 상태 표를 갱신한다.

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
  PT --> CL[분류기<br/>jev · claude]
  PT --> GEN[생성기<br/>claude_text · image_api]
  PT --> RD[렌더러<br/>카드 덱 → PNG]
  PT --> NT[알림<br/>winotify]
  PA --> DRV[브라우저 드라이버<br/>aside · playwright]
  DRV --> Web[(인스타그램 / 스레드 / 네이버 검색)]
  CL --> MD[(MotherDuck prompt_jev)]
  GEN --> API[(Claude API / 이미지 API)]
  U[사용자 브라우저] --> W
```

## 2. 잡과 주기

| 잡 | 주기 | 입력 → 출력 |
|---|---|---|
| health | 30분 | 어댑터 `health()` → 로그아웃 시 알림, 플랫폼 중단 플래그 |
| collect | 하루 2회 ±30분 | 소스 → `source_items`(중복 제거) → 관련성·주제 `classifications` |
| generate | 하루 1회 | 주제 선정(14일 중복 제외) → `contents`(draft → pending_review / approved) |
| publish | 게시 슬롯 | approved 1건 → publishing → 렌더 → 어댑터 → published + `publications` |
| reply | 10분 ±3분 | 새 댓글 → 6분류 → 답글 / 숨김 / 에스컬레이션, `reply_counts` |
| metrics | 하루 1회 | 최근 7일 게시물·계정 지표 스냅샷 |
| weekly_report | 주 1회 | 성과 요약·추천 주제 → 대시보드 + md |

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
| `nsm/cli.py` | 완료 | CLI 테스트 + 수동 | A·C·F·B | doctor(`--live` 실제 API 확인), init, run, run --once, render-preview, install-autostart |
| `nsm/domain/models.py` | 완료 | 단위 | A | 동결. ADR 0009 반영 |
| `nsm/ports/` | 완료 | 단위(fakes 적합성) | A | 동결. `CostSink` 추가 |
| `nsm/store/` (스키마·리포지토리) | 완료 | 단위 | A(스키마), C | `Repository`: 중복 제거, 상태 전이 검사, 게시 재시도 조회, 답글 횟수, 비용 합계, 실행 로그 |
| `nsm/config/` (YAML·keyring·마법사) | 완료 | 단위 | C | 마법사는 `WizardIO`로 입출력 분리, 키는 환경 변수 → keyring 순 |
| `nsm/adapters/sources/` 4종 | 완료 | 단위 + 실사이트 스모크 | D1·Q1 | 업로드·네이버(블로그·뉴스)·구글 트렌드 RSS·커뮤니티. Q1: 텍스트 정리, 관련 글 원문 본문 `fetch_body`(ADR 0022) |
| `nsm/adapters/classifier/` jev·claude | 완료 | 단위(가짜 클라이언트) | D1 | + 키워드 대체(ADR 0011). jev·Claude 실호출은 키 확보 후 확인 필요 |
| `nsm/adapters/generator/` 텍스트·이미지 | 완료 | 단위(가짜 클라이언트) | D2 | Claude(구조화 출력 + fallback), OpenAI 배경. 실호출은 키 확보 후 확인 필요. 네이버 블로그를 게시하지 않으면 블로그 원고 칸을 요청하지 않음(#43, ADR 0026) |
| `nsm/domain/policy.py` | 완료 | 단위 | D1·D2·E | 활동 시간·게시 시간(`should_post`, ADR 0027)·게시 판정·금칙어·배정·템플릿 순환·비용 상한·표기 삽입·댓글 판정 |
| `nsm/domain/labels.py` | 완료 | 단위 | A·B | 표기 문구. 배치 B에서 공정위 예규 원문으로 확정(ADR 0019) |
| 대시보드 화면(`nsm/app/templates`, `static/app.css`, `ko.py`) | 완료 | 라우트(`tests/app`) + 화면 동작(`test_ui.py`) + 전후 스크린샷 | I | 오늘 할 일 중심 개요, 검토 상세 2단, 댓글 카드, 한국어 표시, Pretendard(`/fonts`), 브랜드 강조색, 휴대폰 폭(ADR 0025). 점검 도구 `scripts/dashboard_demo.py`·`dashboard_shots.py` |
| `nsm/app/settings_form.py` + 설정 화면(`/settings`) | 완료 | 라우트·검사(`tests/app/test_settings.py`) + 예약 다시 걸기(`test_scheduler.py`) | M17 | 페르소나·브랜드·운영 값 편집, 저장 즉시 반영(`set_config`), 활동 시간이 바뀌면 `JobScheduler.reschedule`. 계정·드라이런·키는 제외(ADR 0028) |
| `nsm/app/card_editor.py` + 검토 상세 편집 라우트 | 완료 | 라우트(`tests/app/test_card_editor.py`) + 실측 | M16 | 슬라이드별 문구, 배경 올리기·지우기(형식·크기 검사, AI 표기 재계산), 미리보기 렌더(`media/previews`, 잡 스레드). 스타일 선택은 S8 뒤(ADR 0024) |
| `nsm/domain/quality.py` | 완료 | 단위 | Q1 | 품질 규칙: 제외 장르, 카드 목록 번호 떼기, 이모지·딱딱한 표현 검사, 출처 줄(ADR 0023) |
| `nsm/engine/` 잡 7개 | 구현 중 | 경계 1 | D1(collect), D2(generate), E(publish), F(reply·health·metrics), H(weekly_report) | 잡 7개 전부 완료, `runner`가 실행 로그·실패 알림, 재시작 복구 |
| `nsm/adapters/platforms/dryrun.py` | 완료 | 경계 2(드라이런) + E2E | E | 미리보기 저장, 게시 시각 기준 가짜 댓글·지표, 답글·숨김 로그 |
| `nsm/app/` 대시보드 | 구현 중 | 라우트 테스트 + 실서버 스모크 | F·H(백엔드), I(디자인) | 7개 화면(주간 리포트 포함)·CSRF·Host 검사·직접 답글. 화면은 최소 HTML(디자인은 I) |
| `nsm/scheduler/` + autostart | 완료 | 단위 + 실서버 스모크 | F | 작업 스레드 1개, 활동 시간 기준 트리거, 시작프로그램 폴더(ADR 0013) |
| `nsm/adapters/notify/` | 완료 | 수동 | F | winotify 데스크톱 알림 + 로그, 같은 제목 60초 내 중복 억제 |
| `nsm/render/` + `nsm/templates/` | 완료 | 경계 3(렌더) | S-α | `HtmlCardRenderer`(Jinja2 + Playwright, 전용 스레드), 템플릿 3종, Pretendard 동봉. Playwright 없을 때만 단색 대체 |
| `nsm/adapters/drivers/` aside·playwright | 구현 중 | 단위 + 스모크 | D1(수집용 Playwright), B, G1 | 기본값 `aside`(ADR 0017). `aside.py`: repl 실행기·일회용 파일 서버·수집용 `AsideDriver`(G1, ADR 0020). 수집은 Playwright 수집 전용 프로필 유지(ADR 0021) |
| `nsm/adapters/platforms/threads.py` | 완료 | 단위(가짜 실행기) + 실계정 계약(`-m contract`) | G1 | 게시(멱등)·댓글 읽기·답글·숨기기·지표·삭제, 계정 확인. `runtime.make_live_adapters`가 `settings.accounts` 있을 때만 생성 |
| `nsm/adapters/platforms/instagram.py` | 완료 | 단위(가짜 실행기) + 실계정 계약(`-m contract`) | G2 | 게시(4:5 자르기, Threads 동시 공유 끔, 비율 확인, 멱등)·댓글 읽기·답글·지표·삭제. 숨기기 미지원(ADR 0018) |
| `tests/fakes/`, `tests/conftest.py` | 완료 | - | A | 모든 포트의 가짜 + 픽스처 로더 |
| `tests/engine/` 시나리오 | 완료 | 경계 1 | D1·D2·E·F | 수집·생성·게시·대댓글·상태 점검·지표·드라이런 E2E |
| `tests/contracts/` | 계획 | - | S-β, G | 서브 |
| `tests/render/` | 완료 | 경계 3 | S-α | PNG 크기·표기 슬롯·광고 배지·AI 캡션·극단 입력·스레드 충돌 |
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
