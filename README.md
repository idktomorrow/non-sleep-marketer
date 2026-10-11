# 논슬립 마케터 (non-sleep-marketer)

내 PC에서 계속 켜 두면, 내가 정한 AI 가상 인플루언서 페르소나가 자료를 모아 카드뉴스와 글을 만들고, 인스타그램·스레드·네이버 블로그에 올리고, 댓글에 답하고, 성과를 대시보드로 보여주는 로컬 도구입니다. 민감한 글과 댓글은 사람 검토로 넘기고, 가상인물·광고 표기를 자동으로 넣습니다.

> 상태: **드라이런 MVP** (`v0.1-dryrun`, 2026-10-11). 실제 계정 연결(배치 G)과 대시보드 디자인(배치 I)이 남았습니다. 진행 상황은 [architecture.md](docs/architecture.md) 6절.

## 문서 지도

| 알고 싶은 것 | 문서 |
|---|---|
| 무엇을 왜 만드나 | [PRD](docs/PRD.md) |
| 지금 어떻게 돌아가나 | [architecture.md](docs/architecture.md) |
| 쓰는 법 | [user-guide.md](docs/user-guide.md) |
| 처음 합류했다 | [onboarding.md](docs/onboarding.md) |
| 일하는 방식(배치·DoD·기록) | [process.md](docs/process.md) |
| 인터페이스·소유권 약속 | [contracts.md](docs/contracts.md) |
| 이슈 목록 | [issues.md](docs/issues.md) · [GitHub Issues](https://github.com/idktomorrow/non-sleep-marketer/issues) |
| 왜 이렇게 정했나 | [adr/](docs/adr/README.md) · [planning-decisions](docs/planning-decisions.md) · [mvp-review](docs/mvp-review.md) |
| 진행 기록 | [devlog/](docs/devlog/README.md) · [retro/](docs/retro/README.md) · [CHANGELOG](CHANGELOG.md) |
| 모르는 말 | [glossary.md](docs/glossary.md) |

## 빠른 시작

```powershell
uv sync
uv run nsm doctor
uv run nsm init
uv run nsm run      # http://127.0.0.1:8765
```
