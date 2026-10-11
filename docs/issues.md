# 이슈 분리 (MVP, 2명 병렬)

> 작성일: 2026-10-11 · 관련: [PRD](PRD.md) · [contracts.md](contracts.md) · [mvp-review.md](mvp-review.md)
> 담당: **메인** = 선웅제(제품 전부 + 테스트 기반·엔진/대시보드 테스트 + 마지막 UI/UX), **서브** = 팀원(카드뉴스 템플릿·렌더러, 어댑터 계약 테스트, 2주차 품질).
> **진행 단위는 배치입니다.** 이슈→배치 매핑과 일정은 [process.md](process.md) 2절이 원본이고, 아래 "일정 요약" 표는 이전 버전 기록입니다. 2026-10-11 변경: S1·S4·S6은 메인 담당(배치 A·E·F에 포함).
> GitHub Issues 등록 완료(2026-10-11): M0~M15 = #1~#16, S1~S7 = #17~#23. 라벨: `owner:main` / `owner:sub`, `week:1` / `week:2`, `blocking`.

## 병렬 작업 원리

1. **1일차 오전에 계약을 코드로 동결**(M1)합니다. 서브는 그 전에도 [contracts.md](contracts.md) 5·6절의 JSON 계약만으로 템플릿(S2)과 테스트 기반(S1)을 시작할 수 있습니다.
2. 디렉터리 소유권이 겹치지 않습니다. 메인은 `nsm/`의 거의 전부, 서브는 `nsm/render/`, `nsm/templates/`, `tests/`, CI.
3. 서브의 엔진 테스트(S4)는 메인의 엔진(M8)보다 **먼저** 작성해도 됩니다. 가짜 포트에 대고 쓰므로 엔진이 들어오면 바로 통과/실패가 드러납니다.
4. 두 사람의 접점은 세 곳뿐입니다: 포트 변경(합의 PR), 픽스처 형식, `CardRenderer` 포트(서브 구현을 메인 엔진이 호출).

## 일정 요약

| 일차 | 메인 | 서브 |
|---|---|---|
| 1 | M0 골격 → M1 계약 동결 → M2 기술 검증 | S1 테스트 기반(pytest, CI, fakes 뼈대) + S2 템플릿 착수(샘플 덱 작성) |
| 2 | M3 설정·마법사, M4 저장소 | S2 템플릿 3종 디자인 |
| 3 | M5 수집 소스, M6 분류기 | S2 계속, S3 렌더러·하네스 |
| 4 | M7 생성기 | S3 렌더 스냅샷 테스트 |
| 5 | M8 정책·엔진 잡, M9 드라이런 어댑터 | S4 엔진 시나리오 테스트 |
| 6 | M10 검토 대기열·대시보드 백엔드, M11 대댓글 | S4 계속, S5 계약 테스트(드라이런) |
| 7 | M12 스케줄러·운영 → **드라이런 MVP** | S6 라우트 테스트 + 드라이런 E2E |
| 8~12 | M13 실계정 어댑터(스레드 → 인스타 → 네이버) | S5 실계정 계약 테스트 실행·버그 리포트, S7 템플릿 다듬기 |
| 11~13 | M14 지표·주간 리포트 | S7 생성 품질 수동 점검 체크리스트 |
| 13~14 | M15 UI/UX 디자인 패스 | 회귀 테스트, 릴리스 체크 |

---

## 메인 담당

### M0. 저장소 골격과 개발 환경 `week:1` `blocking`
- 이 PC에 Python·uv가 아직 없음(Microsoft Store 스텁만 있음): `winget install astral-sh.uv` → `uv python install 3.12` 먼저.
- `uv` 프로젝트, `pyproject.toml`(python 3.12, ruff, pytest), `nsm/` 패키지 레이아웃(contracts 1절), `README.md`(설치·실행), `.gitignore`(preview/, *.duckdb, 프로필 폴더).
- 완료 기준: `uv sync && uv run nsm --help`가 동작. 서브가 clone 후 `uv run pytest`가 빈 상태로 통과.

### M1. 계약 동결: 도메인 모델·포트·스키마·카드 덱 `week:1` `blocking`
- contracts 2·3·5·8절을 코드로: `nsm/domain/models.py`, `nsm/ports/*.py`, `nsm/store/schema.sql`, `nsm/domain/labels.py`(표기 문구 상수), `nsm/config/schema.py`(페르소나·브랜드 YAML).
- 샘플 덱 JSON이 `CardDeck` 모델로 파싱되는지 확인하는 테스트 1개 포함.
- 완료 기준: 서브가 `from nsm.ports import PlatformAdapter` 등으로 가짜 포트를 만들 수 있음. 이 이슈가 닫히면 포트는 합의 없이 변경 금지.

### M2. 1일차 기술 검증 `week:1` `blocking`
결과를 `docs/day1-verification.md`에 기록.
1. **Aside 무인 게시**: Aside CLI/MCP로 로그인된 스레드 테스트 계정에 글 1개 게시. 승인 프롬프트 없이 끝나는지, Allow 규칙으로 우회되는지. 실패 시 M13 기본 드라이버를 Playwright로 전환.
2. **MotherDuck jev**: Lite 플랜 가입(EU 리전 제외) → `SELECT prompt_jev(...)` 1회. 실패 시 Claude 분류기를 기본으로.
3. **댓글 숨김 메뉴**: instagram.com, threads.net 웹에서 내 게시물 댓글의 "숨기기" 메뉴 존재 여부 수동 확인 → 어댑터 capability 초기값 결정.
4. **인스타그램 프로 계정** 전환(인사이트 필요).
5. **네이버 블로그 글쓰기 화면** 15분 탐색: 스마트에디터 ONE에서 이미지 업로드·본문 입력이 Playwright로 되는지 감만 잡기(2주차 위험 조기 파악).
6. **법규 원문 대조**: 공정위 개정 심사지침 원문·AI 기본법 시행령에서 표기 문구 확인 → `labels.py` 확정.

### M3. 설정·시크릿·페르소나·설정 마법사 `week:1`
- YAML 로딩/검증, `keyring`(Windows 자격 증명 관리자)에 API 키 저장, `nsm init` 마법사(위험 고지 동의 → 브랜드 → 페르소나 → 키 → 플랫폼 연결 "나중에"), 재편집 지원, 전역 `dry_run`.
- 금칙 패턴 기본값에 1인칭 체험 표현("써봤", "먹어보니", "직접 사용해") 포함.
- 완료 기준: 마법사로 만든 YAML이 `tests/fixtures/personas/`와 같은 스키마. 사용자 스토리 1~11.

### M4. 저장소 레이어(DuckDB) `week:1`
- `schema.sql` 적용, 마이그레이션(버전 테이블), 리포지토리 함수(콘텐츠 상태 전이, 댓글, 답글 카운트, 지표, 비용, 로그, settings_kv), `:memory:` 지원, 스레드별 cursor.
- 완료 기준: 상태 전이가 허용된 경로만 통과(예: `published` → `approved` 불가).

### M5. 수집 소스 4종 + 빈도 제한·중복 제거 `week:1`
- `upload`(파일: txt/md/pdf/xlsx → 텍스트), `naver`(통합검색 블로그·뉴스 탭, 키워드당 상위 N), `google_trends`(한국 실시간 트렌드 키워드), `community`(설정에서 켠 사이트만). 웹 소스는 드라이버(Aside/Playwright) 경유, 수집 전용 프로필 선택 가능.
- URL 정규화 해시 중복 제거, 소스별 일일 상한, 호출 간 무작위 지연.
- 완료 기준: `nsm run --once --job collect`가 드라이런에서 `source_items`를 채움. 스토리 12~18.

### M6. 분류기(jev / Claude) `week:1`
- `jev.py`: MotherDuck `prompt_jev` 호출(배치). `claude.py`: 같은 포트, 구조화 출력. 설정으로 선택.
- 프롬프트 3종: 자료 관련성(relevant/irrelevant), 주제(페르소나 topics 중 택1), 댓글 6분류.
- 비용 기록(`costs`). 완료 기준: `tests/fixtures/comments/`의 기대 분류와 80% 이상 일치(수동 확인용 스크립트). 스토리 19~21, 43.

### M7. 생성기(텍스트·이미지) + 표기·금칙어 `week:1`
- Claude 구조화 출력으로 `GeneratedContent`(덱·캡션·스레드 글·블로그 md·claims[]). 템플릿 종류 선택 로직(정보형/리스트형/비교형). 페르소나 말투 시스템 프롬프트. 원문 복사 금지(인용은 20자 이내).
- `ImageGenerator`: 1개 API(Imagen 또는 OpenAI) + `NullImageGenerator`(끄기). 배경 프롬프트에 "no text".
- `apply_labels`, 금칙어·패턴 검사, 비용 기록.
- 완료 기준: 샘플 자료로 세 템플릿 모두 생성되고 claims가 전부 실제 `source_item_id`를 가리킴. 스토리 22~33.

### M8. 정책 규칙 + 엔진 잡 `week:1`
- `policy.py` 순수 함수(contracts 4절), 잡 6개 `run_once(ctx)`, 상태 머신·재시도·보호 모드·쿼터·비용 상한·활동 시간·재시작 복구.
- 완료 기준: 서브의 S4 시나리오 테스트 통과. 스토리 34~41, 47.

### M9. 드라이런 어댑터 + 미리보기 `week:1`
- `dryrun.py`: `publish`는 `preview/YYYY-MM-DD/<content_id>/`에 PNG·caption.txt·threads.txt·blog.md 저장, 댓글·지표는 `tests/fixtures/comments|metrics`(또는 사용자 지정 폴더)에서 시뮬레이션. capability 전부 선언.
- 완료 기준: S5 계약 테스트(드라이런) 통과. 스토리 39.

### M10. 검토 대기열 + 대시보드 백엔드(FastAPI) `week:1`
- 단일 프로세스 안에서 uvicorn 스레드로 서비스. 라우트: 대기열 목록/미리보기/승인/수정/폐기, 넘겨진 댓글 목록/직접 답글/처리 완료, 페르소나 on/off, 자동 게시 on/off, 성과·팔로워·댓글 현황·비용·로그 페이지 데이터.
- HTML은 Jinja2 + HTMX "동작 확인용 최소"(디자인은 M15).
- 완료 기준: S6 라우트 테스트 통과. 스토리 36, 49~53, 56~57.

### M11. 대댓글 파이프라인 `week:1`
- `reply` 잡: 새 댓글 수집 → 분류 → `decide_comment` → 답글 생성(페르소나, 금칙어) / 숨김 / 에스컬레이션 + 데스크톱 알림. 작성자별 일일 3회, 댓글 external_id 중복 방지.
- 완료 기준: 6분류 시나리오 테스트 통과, 드라이런에서 가짜 댓글에 답글 파일 생성. 스토리 42~48.

### M12. 스케줄러·운영(단일 프로세스) `week:1`
- APScheduler로 잡 등록(지터 포함), `nsm run` 부트(스케줄러 + 대시보드 + DB 연결 1개), `health` 잡 + `winotify` 알림, `install-autostart`(schtasks), 실행 로그, `nsm doctor`.
- 완료 기준: `nsm run`을 띄우고 드라이런으로 하루치 흐름이 돌아감 → **드라이런 MVP 완성**. 스토리 56, 58~60.

### M13. 실계정 플랫폼 어댑터 3종 + 드라이버 2종 `week:2`
- `drivers/aside.py`(기본), `drivers/playwright.py`(영구 프로필, 사람이 직접 로그인). 순서: **스레드 → 인스타그램(캐러셀 업로드, 인사이트) → 네이버 블로그(스마트에디터 ONE, 이미지 업로드)**.
- 각 어댑터: publish / fetch_new_comments / reply / hide_comment(가능 시) / metrics. capability 정확히 선언. 로그인 만료 감지.
- 완료 기준: 플랫폼별로 S5 실계정 계약 테스트(`-m contract`) 통과. 네이버는 3주차로 밀릴 수 있음(그 경우 네이버는 드라이런 유지).

### M14. 지표 수집 + 주간 리포트 `week:2`
- `metrics` 잡 실계정 연결, 협찬(광고 표기) 게시물 필터, `weekly_report` 잡(최고 성과·이유·추천 주제·비용) → 대시보드 페이지 + md 파일 저장.
- 스토리 50~55.

### M15. UI/UX 디자인 패스 `week:2`
- 대시보드 전체 화면(대기열, 댓글, 성과, 설정)과 마법사 흐름의 디자인. HTML/CSS 자유 수정, 라우트·데이터 계약은 유지(S6 테스트가 회귀 방지).
- 메인이 마지막에 진행.

---

## 서브 담당

### S1. 테스트 기반: pytest·fakes·픽스처·CI `week:1` `blocking` (→ 메인, 배치 A)
- `tests/conftest.py`(메모리 DuckDB, `FixedClock`, 픽스처 로더), `tests/fakes/`(contracts 7절 전부), `pytest.ini` 마커(`contract`), `.github/workflows/ci.yml`(uv sync → playwright install chromium → pytest).
- 1일차 오전(M1 전)에는 CI와 픽스처 폴더·샘플 덱부터, M1이 머지되면 fakes를 포트에 맞춤.
- 완료 기준: CI 초록, fakes가 모든 포트를 구현(`typing` 검사 통과).

### S2. 카드뉴스 템플릿 3종(정보형·리스트형·비교형) `week:1`
- `nsm/templates/cards/{info,list,compare}.html` + `base.css`, 브랜드 색 토큰(CSS 변수), Pretendard 동봉, 1080×1350, 슬라이드 `kind`별 레이아웃, 배경 없을 때 대체 그라디언트, **표기 슬롯**(`data-label`, `data-sources`, `data-page`) 전부.
- 한글 줄바꿈·긴 제목·긴 bullets(5개)·출처 6개 같은 극단 입력에서 깨지지 않을 것.
- 완료 기준: `tests/fixtures/decks/*.json` 세 덱이 S3 하네스로 렌더되고 메인이 시각 승인.

### S3. 렌더러 + 미리보기 하네스 + 렌더 테스트 `week:1`
- `nsm/render/`: `CardRenderer` 구현(Jinja2로 HTML 생성 → Playwright 크로미움 스크린샷 → PNG), `nsm render-preview` CLI 연결(`cli.py`의 서브 호출만 메인과 합의).
- `tests/render/`: 템플릿별 PNG 존재·크기, 표기 슬롯 DOM 존재, `labels.ad=false`면 광고 배지 없음.
- 완료 기준: 렌더 1덱(5장) 3초 내, 테스트 기본 실행 포함.

### S4. 엔진 경계 1 시나리오 테스트 `week:1` (→ 메인, 배치 E)
`tests/engine/`에 가짜 포트 + 메모리 DB로 작성. 메인의 M8보다 먼저 써도 됨.
- 신뢰도 0.79 → 검토 대기열 / 출처 없는 claim → 검토 / 금칙어 콘텐츠·답글 차단
- 표기: 모든 게시물 첫 줄 가상인물 문구, 광고 표기는 설정 따름
- 댓글 6분류 각각의 처리, 같은 작성자 4번째 답글 차단, 댓글 중복 처리 방지
- 활동 시간 밖·페르소나 off → 게시·답글 없음(health·metrics는 동작)
- 게시 실패 → failed 기록·재시도 간격·3회 후 abandoned
- 보호 모드 첫 20건 검토, 일일 쿼터 초과 시 게시 안 함, 비용 상한 초과 시 생성 중단 + 알림
- 재시작 시 `publishing` 잔여 → 검토 대기열
- 드라이런: 미리보기 폴더에 PNG·원고 생성(M9와 함께)
- 완료 기준: 위 시나리오가 전부 테스트로 존재하고 M8 머지 후 통과.

### S5. 어댑터 계약 테스트 스위트 `week:1`→`week:2`
- `tests/contracts/test_platform_contract.py`: 어댑터 픽스처 파라미터화. 시나리오: publish → URL 반환, fetch_new_comments(since), reply, hide_comment(capability 없으면 NotSupported), metrics 필드 가용성 매트릭스, health.
- 1주차: 드라이런 어댑터로 통과. 2주차: 메인의 실계정 어댑터를 테스트 계정으로 실행하고 실패를 이슈로 보고.

### S6. 대시보드 라우트 테스트 + 드라이런 E2E `week:1` (→ 메인, 배치 F)
- `tests/app/`: 승인/수정/폐기 → 상태 변화, 넘겨진 댓글 처리 완료, 페르소나·자동 게시 토글이 `settings_kv`에 반영.
- E2E: `nsm run --once --job generate` → `--job publish`(드라이런) → preview 폴더 검증 스크립트.
- 완료 기준: 메인의 M15 디자인 작업 중 라우트 회귀를 이 테스트가 잡음.

### S7. 2주차 품질·템플릿 다듬기 `week:2`
- 실계정 계약 테스트 실행·버그 리포트, 템플릿 세부(스레드 썸네일 가독성, 블로그 삽입 시 크기), 생성 결과물 수동 점검 체크리스트(`docs/quality-checklist.md`: 말투 일관성, 출처 정확성, 표기 위치, 한글 깨짐), 릴리스 전 회귀 테스트.

---

## 의존 관계

```
M0 → M1 → { M3, M4, M5, M6, M7, S1(fakes), S3 }
M1(카드 덱 계약) → S2 → S3 → M9(렌더 호출), M7(덱 생성)
M4 → M8 ← { M5, M6, M7 }      S4는 M8과 독립 작성, M8 머지 후 통과
M8 + M9 → M10, M11, M12 → 드라이런 MVP (7일차)
M12 → M13 → M14               S5(실계정) ← M13
M10 → M15 (마지막)             S6 ← M10
```
