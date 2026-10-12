# non-sleep-marketer

로컬 PC에서 단일 프로세스로 도는 AI 가상 인플루언서 마케터. 설계 약속은 `docs/contracts.md`, 진행 방식은 `docs/process.md`, 지금 구현 상태는 `docs/architecture.md`.

## 작업 시작

- "배치 X 실행" 요청을 받으면 `docs/process.md`의 배치 X 절과 DoD를 읽고, DoD를 전부 만족할 때까지 멈추지 않는다. 배치 요청은 브랜치 생성·커밋·푸시·PR 생성 권한을 포함한다.
- 사실(도구, API, 법규)은 직접 조사한다. 사용자에게는 결정만 묻고, 배치 실행 중에는 결정도 묻지 않는다: 합리적 기본값을 고르고 `docs/adr/`에 초안 ADR(상태 `제안`)을 남긴 뒤 계속한다.
- 문서와 커밋 본문은 한국어, 코드 식별자와 커밋 제목은 영어(Conventional Commits: `feat(engine): ...`).

## 소유권

`docs/contracts.md` 1절이 원본. 요약: 메인(선웅제)이 `nsm/`과 `tests/` 대부분, 서브(팀원)가 `nsm/render/`, `nsm/templates/`, `tests/render/`, `tests/contracts/`, `tests/fixtures/decks/`. 남의 디렉터리는 PR로만 고친다.

동결: `nsm/ports/`, `nsm/domain/models.py`, 카드 덱 JSON 계약. 바꾸려면 두 사람이 합의한 PR 하나에서 호출부까지 함께 고친다.

## 완료 기준 (DoD)

배치 PR을 올리기 전에 전부 만족한다.

1. `uv run ruff check . && uv run ruff format --check .` 통과
2. `uv run pytest` 초록 (`contract` 마커 제외가 기본)
3. 새 동작마다 테스트가 있다 (`docs/contracts.md` 10절 경계 기준)
4. `docs/devlog/YYYY-MM-DD-batch-<id>.md` 작성: 목표 / 한 일 / 막힌 것과 해결 / 테스트 결과 / 배운 것 3개(신입 개발자에게 설명하듯) / 다음 배치 메모
5. `docs/architecture.md`의 구현 상태 표 갱신
6. 설계 선택이 있었으면 `docs/adr/` 추가 (템플릿 `docs/adr/0000-template.md`)
7. `CHANGELOG.md` Unreleased에 한 줄
8. `docs/user-guide.md`에서 이 배치가 채우기로 한 절 작성
9. PR 본문 템플릿을 채우고 `/code-review` 지적을 반영

## 하드 가드레일

- main에는 PR로만 반영한다(규칙, GitHub 룰셋은 걸지 않음). 브랜치 이름은 `batch/<id>-<slug>`.
- 시크릿은 `keyring`에만 둔다. 파일·로그·테스트 픽스처에 키를 쓰지 않는다.
- 실계정 어댑터는 `docs/day1-verification.md`에 적힌 테스트 계정에만 게시한다.
- 재론하지 않는 결정: 공식 API 사용, 네이버 블로그 게시 제외(수집은 유지, ADR 0016), Streamlit, 후기형 템플릿 (`docs/adr/` 참고).
