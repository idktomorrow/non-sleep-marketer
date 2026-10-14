# 2026-10-14 배치 S-β: 어댑터 계약 테스트

## 목표

이슈 #21(S5): `tests/contracts/test_platform_contract.py` 하나로 플랫폼 어댑터의 약속을 묶는다. 어댑터 픽스처를 파라미터로 받아 같은 시나리오를 돌린다. 드라이런 어댑터는 기본 실행에 넣고, 실계정은 `-m contract`로만 돌린다. 시작 전에 소유권 이전을 ADR 0034로 기록했다.

## 한 일

- 소유권 이전
  - [ADR 0034](../adr/0034-main-takes-s-beta-gamma-s8.md)(채택): S-β·S-γ·S8을 메인이 맡는다.
  - 요청한 번호는 0030이었지만 이미 알림 ADR이 쓰고 있어서 0034로 했다.
  - 이슈 #21 #23 #34 라벨을 `owner:sub`에서 `owner:main`으로 바꿨다.
  - contracts.md 머리말과 process.md 배치 표의 담당을 갱신했다.
- `tests/contracts/test_platform_contract.py`(새 파일, 기본 25개 + `contract` 6개)
  - **드라이런 어댑터**(threads, instagram 파라미터화)
    - 포트 구현과 capability 표 일치
    - health
    - publish → ID·주소(`http(s)`/`file`, ID에 `/` 없음)
    - fetch_new_comments(since 이후만, 겹침 없음, 모르는 글은 빈 목록, 시각에 시간대)
    - reply → str
    - hide_comment: capability가 있으면 성공, 없으면 `NotSupportedError`
    - 지표 가용성 매트릭스
  - **실계정 어댑터를 가짜 실행기로**
    - 드라이런과 같은 capability
    - 숨기기 미지원이면 화면을 건드리기 전에 `NotSupportedError`
    - health 3가지: 로그인됨, 로그아웃, 다른 계정
  - **실계정 읽기 전용**(`-m contract`)
    - 테스트 계정으로 로그인됐는지 확인
    - 팔로워 수
    - `NSM_CONTRACT_<PLATFORM>_POST`로 준 기존 글의 지표 매트릭스와 댓글 읽기
    - 글을 올리지 않는다. 게시·삭제까지 하는 계약은 기존 `test_threads_live.py`·`test_instagram_live.py`가 맡는다.
- `nsm/adapters/platforms/dryrun.py`: 노출수(impressions)는 인스타그램만 채운다. 스레드 실계정은 노출수를 읽지 않으므로, 드라이런도 같은 필드만 채운다.
- 기존 `test_dryrun_adapter.py`는 그대로 둔다. 세부 동작(미리보기 파일, 가짜 댓글 순서, 블로그)을 보고, 새 파일은 계약만 본다.
- 문서: architecture 구현 상태(`tests/contracts/` 완료, `dryrun.py` 줄), 사용설명서 5절 "연결 점검", CHANGELOG

## 막힌 것과 해결

- **지표 가용성 매트릭스를 무엇으로 정할까.** `PLATFORM_CAPABILITIES`에는 "지표를 읽는다"(post_metrics)까지만 있고 필드 단위 정보는 없다.
  - 그래서 contracts.md 10절 표와 실계정 어댑터가 실제로 읽는 필드로 `METRIC_FIELDS`를 정했다. 인스타그램은 7개 전부, 스레드는 저장·노출수를 뺀 5개다.
  - 이 매트릭스는 capability에서 post_metrics를 선언한 플랫폼에만 있어야 한다. 이것도 테스트로 묶었다.
- **드라이런 스레드가 실계정에 없는 노출수를 채우고 있었다.** 이대로면 드라이런 리포트에서 본 숫자가 실제 운영에서는 "-"로 바뀐다. 드라이런을 실계정에 맞췄다.
- **실계정 계약을 운영 계정에서 돌려도 되는가.** 게시하는 테스트는 운영 계정에 글이 잠깐 보인다. 새 파일의 실계정 케이스는 읽기 전용으로만 만들었다.

## 테스트 결과

- ruff check·format 통과
- `uv run pytest`: 515 passed, 13 deselected
- `uv run pytest -m contract tests/contracts/test_platform_contract.py` 6 passed (38초)
  - 실계정 @nonsleepmarketer에서 읽기만 했다.
  - 스레드 글 DedX7NDCVoz, 인스타그램 글 DedipmzHZAO의 지표·댓글을 읽었다.
  - 게시는 없었다.

## 배운 것 (신입 개발자용)

1. **계약 테스트는 "구현이 여러 개인 인터페이스"를 위한 테스트다.** 엔진은 `PlatformAdapter`만 안다. 그래서 드라이런과 실계정이 같은 약속을 지켜야 드라이런에서 본 흐름이 실제와 같다. 한 시나리오를 구현마다 돌리는 것(pytest 파라미터 픽스처)이 그 약속을 코드로 적는 방법이다.
2. **"없음"과 "0"은 다르다.** 플랫폼이 보여 주지 않는 지표는 `None`이어야 한다. 0으로 채우면 리포트가 "저장 0회"라는 거짓 사실을 말한다. 매트릭스 밖 필드가 None인지 검사하는 이유다.
3. **실계정 테스트는 부작용으로 나눈다.** 읽기만 하는 테스트는 자주 돌려도 된다. 글을 올리는 테스트는 사람이 있을 때만 돌린다. 둘을 파일과 마커로 나눠 두면 실수로 운영 계정에 글이 올라가지 않는다.

## 다음 배치 메모

- S-γ(#23)·S8(#34)도 메인 담당(ADR 0034). 셋이 머지되면 `v0.4-quality` 태그를 붙인다.
- 이슈 #21 본문의 "2주차: 실계정 실패를 이슈로 보고"는 이번 실행에서 실패가 없어 보고할 것이 없다.
