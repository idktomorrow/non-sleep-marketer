# 온보딩 (팀원용)

처음 저장소를 받은 사람이 혼자 시작할 수 있게 쓴 문서입니다. 제품 설명은 [PRD](PRD.md), 사용법은 [user-guide](user-guide.md)에 있습니다.

## 1. 30분 안에 읽을 것 (순서대로)

1. [PRD](PRD.md)의 Problem·Solution 절 (무엇을 만드나)
2. [architecture.md](architecture.md) 1~3절 (어떻게 돌아가나)
3. [process.md](process.md) (어떻게 일하나: 배치, DoD, 기록)
4. [contracts.md](contracts.md) 1절과 내 담당 절 (내가 만질 수 있는 곳)
5. [adr/README.md](adr/README.md) 목록 (왜 이렇게 정했나)
6. [glossary.md](glossary.md) (모르는 말)

## 2. 환경 준비 (Windows)

```powershell
winget install astral-sh.uv
winget install GitHub.cli
uv python install 3.12
gh auth login
git clone https://github.com/idktomorrow/non-sleep-marketer.git
cd non-sleep-marketer
uv sync
uv run playwright install chromium
uv run nsm doctor      # 배치 A 이후. 환경 문제를 진단
uv run pytest
```

Claude Code를 설치하고 저장소 폴더에서 실행하면 루트의 `CLAUDE.md`를 자동으로 읽습니다.

## 3. 일하는 법

- 내 담당 배치는 [process.md](process.md) 2절 표에서 확인합니다. 서브 담당: S-α(템플릿·렌더러), S-β(계약 테스트), S-γ(2주차 품질).
- Claude Code 세션 시작 프롬프트:

```
배치 S-α를 실행해. docs/process.md의 배치 S-α 절과 CLAUDE.md의 DoD를 전부 만족할 때까지 멈추지 마.
브랜치 batch/S-alpha-templates를 만들고 끝나면 PR을 올려.
```

- main에 직접 push하지 않고 PR로 올립니다. 매일 한 번 이상 `git pull origin main`.
- 남의 디렉터리(소유권 표)는 PR로만 고치고 상대에게 리뷰를 요청합니다.
- 포트·도메인 모델·카드 덱 JSON 계약은 동결입니다. 바꿔야 하면 먼저 메인과 이야기합니다.

## 4. 막히면

- 사실(라이브러리 사용법, 플랫폼 동작)은 Claude에게 조사시키고, 결정이 필요한 것만 사람에게 묻습니다.
- 배치 중 결정이 필요하면 기본값으로 진행하고 ADR 초안(상태 `제안`)을 남깁니다. PR 리뷰에서 확정합니다.
