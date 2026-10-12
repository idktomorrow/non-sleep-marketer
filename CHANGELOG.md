# Changelog

[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식. 배치가 머지될 때마다 Unreleased에 한 줄, 태그(`v0.1-dryrun`, `v0.2-live`, `v0.3-mvp`) 때 정리합니다.

## [Unreleased]

### Added
- 배치 H: weekly_report 잡(참여율 상위·이유·추천 주제·협찬 성과·팔로워·비용), 대시보드 주간 리포트 화면 (#15)
- 1주차 회고 초안 `docs/retro/week-1.md`
- 배치 S-α: 카드뉴스 템플릿 3종(정보형·리스트형·비교형)과 HTML 렌더러(1080×1350, 표기 슬롯, Pretendard 동봉, 넘침 시 글자 맞춤), 렌더 테스트, CI 크로미움 설치 (#18 #19)
- 배치 B 준비: 1일차 검증 마법사 `scripts/day1_wizard.sh`(키는 keyring에만, 결과는 `docs/day1-verification.md`), `nsm doctor --live`(실제 API 1회 호출) (#3)

### Changed
- 네이버 블로그 게시를 MVP 범위에서 뺌: 기본 게시 대상은 인스타그램·스레드, 배치 G는 어댑터 2종, 1일차 마법사에서 네이버 에디터 단계 삭제. 네이버 검색 수집은 유지 (ADR 0016)

## [v0.1-dryrun] - 2026-10-11

### Added
- 기획·설계 문서: PRD v2, contracts, issues, process, architecture, user-guide(1절), ADR 0001~0008, 용어집, 온보딩 (2026-10-11)
- 배치 A: 프로젝트 골격(uv, ruff, pytest, CI), 도메인 모델·포트·DB 스키마 동결, 가짜 포트·픽스처, `nsm doctor`, Stop 훅 (#1 #2 #17)
- 배치 C: `nsm init` 설정 마법사, config.yaml 저장, keyring 키 보관, 저장소 레이어(Repository) (#4 #5)
- 배치 D1: 수집 소스 4종(업로드·네이버·구글 트렌드·커뮤니티), 분류기 3종(jev·Claude·키워드), collect 잡, `nsm run --once --job collect` (#6 #7)
- 배치 D2: Claude 콘텐츠 생성기(카드 덱·캡션·스레드·블로그·대댓글), OpenAI 배경 이미지, 게시 판정 정책, generate 잡, DB 스키마 v2 (#8)
- 배치 E: publish 잡(쿼터·간격·재시도·포기·재시작 복구), 드라이런 어댑터(미리보기·가짜 댓글·지표), 표기 삽입·댓글 판정 정책, 엔진 시나리오 테스트 (#9 #10 #20)
- 배치 F: reply·health·metrics 잡, 로컬 대시보드(검토 대기열·댓글·성과·로그, CSRF), 스케줄러(작업 스레드 1개), 데스크톱 알림, 자동 실행, `nsm run` → **드라이런 MVP** (#11 #12 #13 #22)
