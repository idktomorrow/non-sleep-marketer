# 0053. 작업 공간별 설정과 실행 스냅샷

- 상태: 제안 (배치 S13, 서브 제안 PR)
- 날짜: 2026-10-21
- 결정한 사람: 서브(제안 초안). 2026-10-21 사용자 리뷰 반영(작업 공간 격리·실행 컨텍스트 고정·없음/빈 값 구분)
- 관련: [0049](0049-presets-as-workspaces.md)(프리셋 = 작업 공간), [0051](0051-daily-picks-and-saved-drafts.md)(오늘의 추천·별칭), [0054](0054-openai-single-key-and-cost-ledger.md)(공급자·비용), contracts.md 1절

## 배경

C29(ADR 0049)로 프리셋이 "페르소나+브랜드+DB 파일" 작업 공간이 됐다. 그런데 S11이 넣은 검색 별칭·하루 추천 수는 `settings.picks`(전역 운영 설정)에 있어, 치이카와 프리셋의 별칭이 롤 프리셋에서도 검색에 들어가고 추천 구성도 공유됐다. 앞으로 넣을 정보/밈 추천 개수·제작 모드·기본 캐릭터·디자인 옵션도 분야마다 달라야 한다.

또 어댑터(생성기·분류기·이미지)는 시작 때 한 번 만들어져 키를 바꾸면 재시작이 필요했고, 대시보드 설정 저장은 `ctx.config`만 바꿔 실행 중인 잡이 도중에 다른 설정을 볼 수 있었다.

## 선택지

1. **전부 전역에 두고 known-gaps에 적기** / 프리셋을 바꿀 때마다 사람이 별칭·개수를 손으로 고쳐야 하고, 자동 추천이 다른 분야 별칭으로 검색한다.
2. **프리셋 DB(`settings_kv`)에 두기** / DB가 작업 공간별이라 격리는 되지만 설정 화면·마법사·백업과 따로 놀고, 프리셋 파일만 복사해서는 옮겨지지 않는다.
3. **`AppConfig.workspace` 절을 두고 preset.yaml에 함께 저장·복원 (채택)** / 설정 화면·파일·프리셋이 한 모양. 전역과 작업 공간의 경계를 문서로 못 박아야 한다.

어댑터 갱신은 (a) 속성을 하나씩 바꾸기(실행 중 잡이 반쯤 바뀐 묶음을 볼 수 있음) 대신 (b) **새 묶음(`Adapters`)을 만든 뒤 한 번에 바꾸고, 잡은 시작 때 스냅샷을 받아 끝까지 그것만 쓴다 (채택)**.

## 결정 (제안)

- **작업 공간별(`workspace`)**: `aliases`, `info_count`, `meme_count`, `allow_substitute`, `production_mode`, `hero_default`, `sidekick_default`, `title_style`, `color_from_photo`. `persona.topics`는 원래 프리셋 소속. "오늘만 변경"(S14)은 프리셋 DB의 `settings_kv`.
- **전역(`settings`)**: API 키(keyring), `text_provider`, `models`, `daily_cost_cap_usd`, 플랫폼·쿼터·스케줄·`picks.enabled`·`fresh_hours`·`photo_candidates`.
- **호환**: 예전 설정(workspace 절이 **없음**)은 `picks.count`·`picks.aliases`를 workspace로 가져온다. 절이 있으면 `aliases: {}`는 사용자가 비운 값이므로 그대로 둔다(`model_fields_set`로 "없음"과 "빈 값"을 구분, `schema_version` 필드). 예전 프리셋 파일을 불러올 때는 지금 설정을 끌고 가지 않고 기본값으로 시작한다(다른 프리셋 별칭이 섞이지 않게).
- **실행 스냅샷**: `Adapters` 묶음은 `make_adapters()`가 만들고 `ctx.apply_adapters()`로 한 번에 바꾼다. `run_job`은 `ctx.snapshot()`(설정 복사 + 묶음 참조)을 잡에 넘긴다. 키 등록·공급자/모델/플랫폼 설정 저장·프리셋 전환 뒤 `refresh_adapters()`가 작업 스레드에서 새 묶음을 만든다(브라우저 드라이버 자원은 거기서만). 재시작 불필요.
- 제작 작업·슬롯 행(S14·S15)은 `preset` 이름을 기록해 전환 뒤 다른 공간의 작업을 실행하지 않는다.

## 결과

- 프리셋을 바꾸면 별칭·추천 구성이 같이 바뀌고, 키를 등록하면 다음 작업부터 쓴다. 진행 중인 잡은 영향을 받지 않는다.
- 어려워진 것: 설정 항목마다 "전역인가 작업 공간인가"를 정해야 한다(위 목록이 기준). 미디어 폴더(`media/`, `uploads/`)는 전역이고 content id로 귀속된다.
- 테스트: `tests/unit/test_workspace_settings.py`(호환·빈 값·프리셋 왕복), `tests/unit/test_provider_selection.py`(스냅샷 격리).
