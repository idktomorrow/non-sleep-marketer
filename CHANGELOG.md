# Changelog

[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식. 배치가 머지될 때마다 Unreleased에 한 줄, 태그(`v0.1-dryrun`, `v0.2-live`, `v0.3-mvp`) 때 정리합니다.

## [Unreleased]

### Added
- 기획·설계 문서: PRD v2, contracts, issues, process, architecture, user-guide(1절), ADR 0001~0008, 용어집, 온보딩 (2026-10-11)
- 배치 A: 프로젝트 골격(uv, ruff, pytest, CI), 도메인 모델·포트·DB 스키마 동결, 가짜 포트·픽스처, `nsm doctor`, Stop 훅 (#1 #2 #17)
- 배치 C: `nsm init` 설정 마법사, config.yaml 저장, keyring 키 보관, 저장소 레이어(Repository) (#4 #5)
- 배치 D1: 수집 소스 4종(업로드·네이버·구글 트렌드·커뮤니티), 분류기 3종(jev·Claude·키워드), collect 잡, `nsm run --once --job collect` (#6 #7)
