# 0057. 추천 슬롯(pick_slots): 정보 N + 밈 M 계획과 제작 상태 기계

- 상태: 제안 (배치 S14, 서브 제안 PR)
- 날짜: 2026-10-21
- 결정한 사람: 서브(제안 초안). 2026-10-21 사용자 리뷰 반영(전이 완결·목표 축소·트랜잭션·requested/actual·마이그레이션·재시도는 next_attempt_at)
- 관련: [0051](0051-daily-picks-and-saved-drafts.md)(picks), [0053](0053-workspace-settings-and-run-snapshot.md), [0056](0056-meme-path-and-scenario-writer.md), contracts.md 2절(콘텐츠 상태 동결)

## 배경

S11의 추천은 "완성된 content가 있어야 pick 행이 있다"라 기획·제작 중·실패가 살 곳이 없었고, `count_for_date`가 상태 불문 전부를 세며 수동 실행은 상한을 넘겼다. 정보 2개가 끝나고 밈만 실패했을 때 밈만 이어서 만들려면 슬롯 단위 상태가 필요하다. 콘텐츠 상태(동결)는 건드리지 않는다.

## 선택지

1. **picks 행에 상태 컬럼 추가** / content 없이는 행이 없어 "계획됨·실패"를 못 적는다.
2. **메모리 큐** / 재시작하면 사라지고 동시 실행을 막지 못한다.
3. **`pick_slots` 표(계획부터 존재, 키 = 날짜+종류+번호) (채택)** / DB가 작업 공간별이라 키가 충분하고, 행에 `preset`을 적어 전환 뒤 교차 실행을 막는다.

## 결정 (제안)

- 표 `pick_slots(run_date, requested_kind, slot_no PK, actual_kind, stage, content_id, preset, run_id, attempts, next_attempt_at, last_error, plan_json, cast_json, mix)`. `picks`에 `kind`·`slot_key`.
- **단계**: planned → writing → written → images_pending → producing → ready. 실패 `failed_text`(← writing, 재시도 → planned), `failed_image`(← producing, 재시도 → images_pending, **content·자료 유지**), `empty`(후보 없음 → 재시도는 planned), `unknown_image`(S15, 사람 결정), `cancelled`(목표 축소). `attempts` ≤3.
- **계획**: `effective_plan` = 오늘만 변경(프리셋 DB `settings_kv["picks.plan_override"]`, 날짜가 오늘일 때만) 아니면 `workspace.info_count/meme_count`. `SlotStore.plan`은 부족분만 만들고, 목표를 낮추면 **content 없는 초과 슬롯만** cancelled(완성 결과 유지), 다시 올리면 되살린다. 둘 다 0이면 잡은 건너뛴다.
- **실행**: `claim`(조건부 UPDATE planned→writing)으로 슬롯을 잡고 생성. 저장은 contents + 자료 used + picks + 슬롯 연결 **한 트랜잭션**(`PicksStore.save(slot_key=…)`). 장애는 `fail(retry_in=15분)` → planned + `next_attempt_at`; `slots_retry` 잡(15분)이 due 슬롯만 다시(sleep 없음). 수동 실행도 슬롯 안에서만.
- **대체**: `workspace.allow_substitute`일 때만 빈 슬롯을 다른 종류로 채우고 `requested_kind≠actual_kind`로 기록·표시.
- **마이그레이션**(스키마 v5): 기존 `picks` 행마다 `pick_slots(run_date,'info',rank, stage=ready)`를 만들어 배포 당일 재생성을 막는다.
- **재시작 복구**: 시작 시 `recover_stale`(writing→planned, producing→images_pending, 2시간 기준).
- 게시 관문: 슬롯이 images_pending/producing/failed_image/unknown_image면 승인·게시 거부(409).

## 결과

- "정보 2 + 밈 1"처럼 자유 구성, 실패한 종류만 이어서, 같은 날 재실행 멱등, 전환 뒤 다른 공간 슬롯은 집지 않음.
- 어려워진 것: 상태가 picks(보관·게시)와 pick_slots(제작) 두 곳에 나뉜다(역할이 다르므로 섞지 않는다). 수동 "지금 추천 만들기"가 상한을 넘겨 더 만들지 않는다(원하면 오늘 구성을 올린다).
- 테스트: 계획 멱등·축소·되살림, claim 배타·재시도 시각, 실패→복귀 단계·횟수 상한, 재시작 복구·작업 공간 필터, v4→v5 마이그레이션, 엔진(정보 2+밈 1, 축소 시 유지, 밈 실패 격리·재시도, 공급자 없음, 대체 기록, 오늘만 변경, 0 계획).
