# 0021. 자료 수집은 Playwright 수집 전용 프로필을 유지한다 (게시·댓글만 Aside)

- 상태: 채택 · 날짜: 2026-10-13 · 결정: 선웅제 · 관련: [0002](0002-aside-default-driver-split.md), [0017](0017-aside-default-driver-after-day1.md), [0020](0020-aside-repl-execution.md)

## 배경

[0017](0017-aside-default-driver-after-day1.md)은 브라우저 드라이버 기본값을 Aside로 정했다. 배치 G에서 게시·댓글·지표는 Aside(`aside repl`)로 구현했다. 수집용 `AsideDriver`도 만들었지만 `make_driver("aside")`는 Playwright로 대신 돌게 두었다. 수집까지 Aside로 바꿀지는 따로 정하기로 했다.

## 선택지

1. **수집도 Aside**: 드라이버 하나로 통일 / 사용자가 쓰는 Aside 브라우저에 검색 탭이 하루 여러 번 열렸다 닫힘
2. **수집은 Playwright 수집 전용 프로필 유지**: 사용자 화면을 방해하지 않음. 네이버 검색·구글 트렌드·커뮤니티 수집은 로그인이 필요 없어 Aside의 장점(로그인된 브라우저)이 쓸모없음

## 결정

2번. 수집은 Playwright 수집 전용 프로필(`~/.nsm/collect-profile`)로 계속 한다. 이유: Aside 브라우저에 검색 탭이 열리는 불편, 수집은 로그인 불필요.

## 결과

- `settings.browser_driver`는 실계정 게시·댓글용 드라이버(Aside)를 뜻한다. 수집은 이 값과 상관없이 Playwright를 쓴다(`make_driver`).
- 수집 계정과 게시 계정이 분리된 상태가 유지된다(0002의 "수집 전용 계정" 취지).
- `nsm/adapters/drivers/aside.py`의 `AsideDriver`는 남겨 둔다. 로그인이 필요한 수집 소스가 생기면 그때 쓴다.
