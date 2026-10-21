# 0054. OpenAI 키 하나로 글·답글·그림: 공급자 분리, 오류 정책, 전역 비용 원장 (포트 추가, 합의 필요)

- 상태: 제안 (배치 S13, 서브 제안 PR. `nsm/ports/`에 `ImageStudio`·`CostLedger`를 더하므로 **동결 계약 변경, 메인·서브 합의 전 머지 금지**)
- 날짜: 2026-10-21
- 결정한 사람: 서브(제안 초안). 2026-10-21 사용자 리뷰 반영(재시도 분리·unknown·비용 구분·전역 원장)
- 관련: [0053](0053-workspace-settings-and-run-snapshot.md), [0036](0036-card-design-styles.md)(AI 배경), [0048](0048-gemini-classifier-replaces-jev.md)(분류기 체인), contracts.md 3절

## 배경

글 생성은 Claude 전용이었고 OpenAI 키는 표지 배경 한 장에만 쓰였다. 사용자는 OpenAI 키 하나로 정보 원고·캡션·밈 대본·보조 일러스트·만화 컷까지 만들고 싶어 한다. 앞으로의 이미지 제작(S15)은 유료 호출을 여러 번 하므로, 오류·재시도·비용을 어설프게 다루면 과금이 불명확한 상태에서 중복 호출이 생긴다.

공식 문서(developers.openai.com, 2026-10-21)로 확인한 것: Responses API `text.format` strict JSON 스키마(전 필드 required, `additionalProperties:false`), `status=incomplete`/`refusal` 응답, 429의 `error.code`가 한도(`rate_limit_error`)와 크레딧(`insufficient_quota`·`credit_balance_exhausted`·`*_spend_limit_exceeded`)을 구분함, Images API `/images/edits`의 `image[]` 참조 최대 16장·`background=transparent`·크기 규칙(16 배수, 1:3~3:1), 이미지 가격이 이미지 입력·텍스트 입력·출력 토큰별로 다름($8·$5·$30 /1M).

## 선택지

1. **Claude 필수 유지 + OpenAI는 이미지만** / 사용자가 키 두 개를 사야 한다.
2. **OpenAI SDK 도입** / 의존성·자동 재시도가 SDK 안에 숨어 전송 전/후 구분이 어렵다.
3. **httpx 직접 호출 + 공용 프롬프트 모듈 (채택)** / 요청을 우리가 쥐고 있어 오류 단계별 정책을 코드로 둘 수 있다. 기존 Gemini 어댑터와 같은 방식.

## 결정 (제안)

- **공급자**: `settings.text_provider ∈ auto|openai|claude`. auto = Claude 키 → Claude, 아니면 OpenAI 키 → OpenAI(기존 사용자 동작 유지). 명시하면 그 공급자만 쓰고 키가 없으면 생성하지 않고 이유를 보인다(조용한 전환 금지). 분류기는 Gemini→(Claude)→낱말 규칙 그대로, 이미지 자료 읽기도 같은 규칙으로 OpenAI 경로 추가.
- **공용 프롬프트** `prompts.py`: 스키마·시스템/사용자 프롬프트·덱 조립을 Claude·OpenAI가 함께 쓴다. 깨진 JSON은 두 공급자 모두 `AdapterError`(이 자료만 건너뜀).
- **오류 정책(재시도는 호출자가, 공용 클라이언트는 분류만)**:
  - 전송 전 실패(연결·DNS)·한도·과부하 → 재예약 가능(`TransientError`/`ImageTransient`, `Retry-After` 존중, sleep 금지).
  - 키·크레딧(auth/quota) → 영구 실패(`ProviderUnavailable`/`ImageUnavailable`) + 알림.
  - 정책 차단(moderation) → `ImageRefused`(사유 표시).
  - **전송 뒤 응답 없음**: 텍스트는 1회만 다시 보내고 첫 요청 비용은 미확인으로 남김. **이미지는 자동 재시도 없이 `ImageUnknown`**(생성·과금 불명) → 사람이 "다시 요청"을 누른다.
- **전역 비용 원장** `CostLedger`(`<NSM_HOME>/costs.duckdb`): 작업 공간을 바꿔도 하루 상한은 하나. 유료 호출 전 `reserve(추정액)` → 응답 usage로 **같은 행**을 `confirm`(이중 합산 없음) / 전송 전 실패 `release` / 전송 뒤 응답 없음 `mark_unknown`(예약액을 계속 셈, 사람이 풀 때까지). 상한 집계 = confirmed + estimated + reserved/unknown 예약액. 작업 공간 DB의 `costs`는 그 공간 표시용 사본. 비어 있으면 기존 `costs`를 한 번 옮긴다.
- **비용 단가**: 텍스트는 모델별 입력/출력 단가, 이미지는 `usage.input_tokens_details`의 이미지/텍스트 토큰과 출력 토큰에 각각 단가(확정). 상세가 없으면 품질별 추정액으로 `estimated` 표시.
- **포트 추가(동결 변경)**: `ImageStudio.make(ImageSpec) -> ImageResult`, `CostLedger(reserve/confirm/release/mark_unknown/spent_between)`. `ImageGenerator`(표지 배경)·`CostSink`는 그대로.
- **키 화면**: 등록 여부만 표시, 등록·삭제는 keyring에만, 값은 응답·로그에 없음. 등록 즉시 어댑터 묶음 갱신(ADR 0053). 키 등록 자체는 유료 그림 생성 허용이 아니다(제작 모드 `plan_first` 기본, S15).

## 결과

- OpenAI 키 하나로 원고·답글이 된다. 그림 제작기는 준비됐고 실제 호출은 S15 제작 파이프라인이 한다.
- 어려워진 것: 포트가 둘 늘어 합의가 필요하다. 미확인 요청은 서버 측 결과 조회 API가 없어 사람이 정리한다. Responses 캐시 할인은 반영하지 않는다(보수적).
- 테스트: `tests/unit/test_openai_text.py`·`test_openai_image_studio.py`·`test_cost_ledger.py`·`test_provider_selection.py`, `tests/app/test_keys_routes.py`.
