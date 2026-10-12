# 0017. 1일차 검증 후 브라우저 드라이버 기본값을 Aside로 되돌린다

- 상태: 제안 (배치 B 반영, Claude가 검증 결과로 결정) · 날짜: 2026-10-13 · 관련: [0002](0002-aside-default-driver-split.md), [0010](0010-collect-driver-default-playwright.md)(대체), [day1-verification](../day1-verification.md), 이슈 #3 #14

## 배경

[0002](0002-aside-default-driver-split.md)는 Aside를 기본 드라이버로 정했다. [0010](0010-collect-driver-default-playwright.md)은 Aside가 사람 승인 없이 동작하는지 모르므로 배치 B 전까지 Playwright를 기본으로 두었다. 2026-10-13 1일차 검증 결과:

- Aside CLI 1.26으로 스레드 글과 인스타그램 이미지 게시가 **승인 창 없이 끝까지** 됐다(57초, 84초).
- 파일은 Aside의 읽기 허용 폴더(File permissions > Can view)에 있어야 한다. 밖에 있으면 Aside가 우회하지 않고 멈춘다.
- 인스타그램 업로드의 기본 자르기가 1:1이라 4:5 카드가 잘렸다.
- 자연어 명령은 한 번에 1분 안팎 걸리고 Aside의 모델 사용량을 쓴다.

## 선택지

1. **Playwright 유지(0010 채택)**: 이미 구현돼 있다 / 로그인 세션을 따로 관리해야 하고, 0002의 결정과 어긋난다
2. **Aside를 기본값으로, 구현 전까지는 Playwright로 대신 동작**: 사용자는 Aside 브라우저에 한 번만 로그인하면 된다 / 배치 G에서 드라이버를 구현해야 한다

## 결정

2번. `settings.browser_driver` 기본값을 `aside`로 바꾼다. `make_driver("aside")`는 배치 G 전까지 Playwright 드라이버를 돌려줘 수집이 멈추지 않게 한다.

## 결과

- 배치 G에서 Aside 드라이버를 구현한다. 메모:
  - 매번 결과가 같아야 하므로 자연어 `aside "..."` 대신 `aside repl`(Playwright 형식 스크립트)로 정해진 순서를 실행한다. 자연어는 느리고 매번 동작이 달라질 수 있다.
  - 카드 PNG는 NSM 폴더(`~/.nsm`)에 두고, 사용자가 Aside 설정 Can view에 이 폴더를 추가한다(`nsm doctor`가 안내). → 배치 G1에서 바뀜: repl에는 Can view가 적용되지 않아 일회용 로컬 서버로 넘긴다([0020](0020-aside-repl-execution.md)).
  - 인스타그램은 자르기 단계에서 4:5를 고르고, 올린 뒤 배지가 잘리지 않았는지 확인한다.
  - 로그인된 계정이 `settings.accounts`의 핸들과 다르면 게시하지 않는다.
- `nsm doctor`에 Aside CLI 점검을 추가했다.
- 다시 볼 조건: Aside CLI가 무인 실행을 막도록 바뀌면 0010처럼 Playwright로 돌아간다(설정 한 줄).
