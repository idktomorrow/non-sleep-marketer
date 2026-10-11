# 진행 방식 (배치·DoD·기록)

> 작성일: 2026-10-11 · 관련: [issues.md](issues.md) · [contracts.md](contracts.md) · [architecture.md](architecture.md) · 결정 이력: [adr/](adr/README.md)

## 1. 배치란

배치 = 이슈 묶음 1개 = Claude Code 세션 1회 = 브랜치 `batch/<id>-<slug>` 1개 = PR 1개.
배치는 **결정 없이** 끝난다. 결정은 배치 전에 끝내고(3절 사전 결정, contracts.md), 배치 중 새 선택지가 생기면 Claude가 기본값을 고르고 ADR 초안을 남긴다. 사용자는 PR 리뷰에서 뒤집는다.

사람만 할 수 있는 일(가입, 로그인, 결제, 수동 확인, 디자인 승인)은 배치 B·G와 PR 리뷰 시점에만 모아 둔다.

### 세션 시작 프롬프트

```
배치 D1을 실행해. docs/process.md의 배치 D1 절과 CLAUDE.md의 DoD를 전부 만족할 때까지 멈추지 마.
브랜치 batch/D1-collect-classify를 만들고 끝나면 PR을 올려.
```

### PR 흐름

브랜치 → 작업 → DoD 자체 점검 → `/code-review` → 지적 반영 → 푸시 → PR(템플릿) → CI 초록 → 사용자가 PR 본문·devlog·ADR 읽고 머지(셀프 머지 허용, 2명이라 승인 요구 없음).

main 직접 push 금지와 PR 필수는 팀 규칙이다. GitHub 룰셋은 걸지 않았다(2026-10-11 결정). 필요해지면 배치 A 머지 후 CI 필수 체크와 함께 건다.

## 2. 배치 목록

1주차 메인은 A·B·C·D1·D2·E·F 7개(B는 사람 주도). 서브는 S-α·S-β. 2주차는 G·H·I·S-γ.

| 배치 | 이슈 | 담당 | 일정 | 사람 개입 | 끝났을 때 보이는 것 |
|---|---|---|---|---|---|
| **A 골격·계약·테스트 기반** | M0 #1, M1 #2, S1 #17 | 메인(Claude) | 1일차 오전 | 시작 전 uv 설치 | `uv run nsm --help`, 빈 pytest 초록, CI 초록, 포트·모델·스키마·카드 덱 모델 동결, fakes 뼈대, `uv.lock` 커밋, Stop 훅(아래) |
| **B 1일차 검증** | M2 #3 | 메인(사람 + wizard 스킬) | 1일차 오후 | 전부 | `docs/day1-verification.md`, `labels.py` 문구 확정, 드라이버 기본값 ADR |
| **C 설정·저장소** | M3 #4, M4 #5 | 메인(Claude) | 2일차 | 없음 | `nsm init` 마법사, keyring 저장, 스키마·리포지토리·상태 전이 테스트, user-guide 2절 |
| **D1 수집·분류** | M5 #6, M6 #7 | 메인(Claude) | 3일차 | 없음(키는 B에서 저장) | `nsm run --once --job collect`(드라이런)로 자료 수집·분류 저장 |
| **D2 생성** | M7 #8 | 메인(Claude) | 4일차 | 없음 | `--job generate`로 세 템플릿 콘텐츠 생성, claims 전부 출처 연결 |
| **E 정책·엔진·드라이런·시나리오 테스트** | M8 #9, M9 #10, S4 #20 | 메인(Claude) | 5일차 | 없음 | S4 시나리오 전부 통과, preview 폴더에 원고. **대체 규칙**: S-α가 아직 머지되지 않았으면 `FakeRenderer`로 DoD를 만족하고 PNG 검증은 S-α 머지 후 E-후속 커밋으로 |
| **F 대시보드·대댓글·스케줄러** | M10 #11, M11 #12, M12 #13, S6 #22 | 메인(Claude) | 6~7일차 | 없음 | `nsm run`으로 하루치 드라이런 흐름, 라우트 테스트, user-guide 3·4절 → 태그 `v0.1-dryrun` |
| **S-α 템플릿·렌더러** | S2 #18, S3 #19 | 서브(Claude) | 1~4일차 | 디자인 승인은 PR 리뷰에서 | 3템플릿 PNG, 렌더 테스트, `nsm render-preview` |
| **S-β 계약 테스트** | S5 #21 | 서브(Claude) | 5~6일차 | 없음 | 드라이런 어댑터 계약 테스트 통과 |
| **G 실계정 어댑터** | M13 #14, PR 3개(스레드 → 인스타그램 → 네이버) | 메인(사람 로그인 + Claude) | 8~12일차 | 테스트 계정 로그인 | 플랫폼별 `-m contract` 통과, user-guide 5절. 시작 전 `/security-review` 1회 |
| **H 지표·주간 리포트** | M14 #15 | 메인(Claude) | 11~13일차 | 없음 | 주간 리포트 페이지·md → 태그 `v0.2-live` |
| **I UI/UX 패스** | M15 #16 | 메인(사람 + Claude) | 13~14일차 | 디자인 결정 | 라우트 테스트 회귀 없이 화면 완성 → 태그 `v0.3-mvp` |
| **S-γ 2주차 품질** | S7 #23 | 서브 | 11~14일차 | 수동 점검 | 품질 체크리스트, 회귀 테스트 |

배치 A의 Stop 훅: `.claude/settings.json`에 Stop 훅으로 `uv run pytest -q`를 등록한다. 조건: `stop_hook_active`가 true면 즉시 통과(무한 루프 방지), 현재 브랜치가 `batch/`로 시작할 때만 실행, pyproject가 없으면 통과. 이 훅은 저장소에 pyproject가 생기는 배치 A PR에 포함한다.

## 3. 사전 결정 (배치 중 묻지 않기 위해 고정)

- Python 3.12, `uv`, `ruff`(lint + format), `pytest`, `pydantic` v2, `pyyaml`, `keyring`, `duckdb`, `apscheduler` 3.x, `fastapi` + `uvicorn` + `jinja2` + HTMX(`nsm/app/static/`에 동봉), `playwright`, `anthropic`, `winotify`, `typer`, `httpx`, `python-ulid`.
- 이미지 생성: OpenAI Images 어댑터 1종 + `NullImageGenerator`. Imagen은 2주차 선택.
- Claude 모델: 생성·답글 `claude-sonnet-5-5`, 분류 대체 구현 `claude-haiku-5-5`. 모델 ID는 설정값.
- 로그: 표준 `logging` + `run_logs` 테이블.
- 시간대: 설정 `timezone`(기본 `Asia/Seoul`), DB는 UTC.
- ID: ULID 문자열. 커밋: Conventional Commits, 제목 영어, 본문 한국어 허용.
- 의존성 고정: `uv.lock` 커밋, CI는 `uv sync --frozen`.
- 릴리스 태그: `v0.1-dryrun`(F), `v0.2-live`(H), `v0.3-mvp`(I). 태그마다 CHANGELOG 정리.
- 온보딩 첫 명령은 `nsm doctor`.

## 4. 기록 체계

| 기록 | 위치 | 언제 | 목적 |
|---|---|---|---|
| ADR | `docs/adr/NNNN-slug.md` | 설계 선택마다. 기존 결정은 0001~0008 | 왜 이렇게 했나 |
| 배치 devlog | `docs/devlog/YYYY-MM-DD-batch-<id>.md` | 배치 PR 올릴 때 | 흐름·막힘·배운 것(신입 학습) |
| PR 본문 | `.github/pull_request_template.md` | PR마다 | 리뷰 기록 |
| 1일차 검증 | `docs/day1-verification.md` | 배치 B | 외부 의존성 사실 |
| 용어집 | `docs/glossary.md` | 배치마다 추가 | 공용 어휘 |
| 온보딩 | `docs/onboarding.md` | 지금 | 팀원 혼자 시작 |
| 사용설명서 | `docs/user-guide.md` | C·F·G·H에서 절 채움 | 최종 사용자 관점 |
| 구현 아키텍처 | `docs/architecture.md` | 배치마다 상태 표 갱신 | 지금 실제 구조 |
| CHANGELOG | `CHANGELOG.md` | 배치 머지마다 | 변경 이력 |
| 주간 회고 | `docs/retro/week-N.md` | 7·14일차 | 프로세스 개선 |
| 학습 색인 | `docs/learning-index.md` | 14일차 | devlog의 "배운 것" 모음 |
