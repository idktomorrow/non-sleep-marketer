# 0051. 덱 JSON에 상품 그리드(products)·정보 패널(facts) 장과 소식형(editorial) 템플릿을 더한다 (동결 계약 변경 제안)

- 상태: 제안 (배치 S12, 서브 제안 PR. **동결 계약 변경이라 메인·서브 합의 전에는 머지하지 않는다**)
- 날짜: 2026-10-20
- 결정한 사람: 서브(제안 초안). 계약 변경은 두 사람이 합의한 PR 하나에서 호출부까지 함께(contracts.md 1절)
- 관련: [0050](0050-editorial-style-verified-photos.md)(사진 우선형 스타일), [0051](0051-daily-picks-and-saved-drafts.md)(오늘의 추천), [0037](0037-per-content-card-style.md)(원고별 스타일, 제안), [0024](0024-card-editor-scope.md)(카드 편집), contracts.md 5절

## 배경

굿즈·협업 소식 카드에는 "상품 2~4개 사진·이름·가격"과 "출시일·구매처·수량 제한" 장이 거의 항상 들어간다(2026-10-20 사용자 레퍼런스). 지금 덱 JSON은 장마다 `background` 하나뿐이고 항목별 그림·가격 칸이 없으며, 템플릿마다 가운데 장 종류가 하나로 고정돼(`info→body`, `list→list`, `compare→compare`) 이런 장을 그릴 수 없다. 사진 우선형 스타일(S10)과 오늘의 추천(S11)만으로는 표지·본문·마무리까지가 한계다.

## 선택지

1. **기존 장으로 흉내 내기**: `list` 항목에 "이름 ¥가격"을 써 넣는다 / 그림·가격 배지가 없고, 가격이 글 속에 묻혀 틀려도 검사할 수 없다.
2. **상품·일정 정보를 덱 밖 별도 표에 두기**(원고별 스타일 ADR 0037의 2번 안과 같은 구조) / 같은 원고 정보가 두 곳에 나뉘고 렌더 캐시 지문·편집·게시가 모두 따로 조회한다.
3. **덱 JSON에 장 종류 두 개와 템플릿 하나를 더한다 (채택, 합의 필요)**: 그림은 장별 `background`처럼 로컬 경로, 글자는 HTML. 기존 JSON은 전부 그대로 읽힌다(필드 추가뿐) / 동결 계약 변경이라 모델·계약 문서·생성기·편집 화면·렌더러·픽스처를 한 PR에서 같이 고쳐야 한다.

## 결정 (제안)

3번. `nsm/domain/models.py`에 다음을 더한다.

- `ProductItem(name, price="", note="", image=None)` — 가격은 자료 표기 그대로 문자열(통화·단위를 코드가 해석하지 않는다). `image`는 사람이 올린 파일 경로.
- `ProductsSlide(kind="products", heading, items: 2~4개, background)`
- `FactRow(label, value)`, `FactsSlide(kind="facts", heading, rows: 2~6줄, background)`
- `TemplateKind`에 `editorial`. `_shape`의 허용표에 `"editorial": {"body", "products", "facts"}` — 가운데 장을 섞어 쓸 수 있는 유일한 템플릿이다. 기존 세 템플릿은 그대로.

호출부(한 PR에서 같이):
- **생성기** `claude_text.py`: 슬라이드 스키마에 `products`(name·price·note)·`facts`(label·value) 칸, `_TEMPLATE_GUIDE["editorial"]`, `build_deck`의 editorial 분기(상품 2개 미만이면 버리고, 둘 다 없으면 body). 템플릿 후보는 **사진 우선형 스타일일 때만** `editorial`을 앞에 더한다(`choose_template(recent, templates)`).
- **사실 확인** `quality.unverified_products()`: 상품 이름·가격(숫자)이 자료 본문에 없으면 사유를 붙여 **자동 승인하지 않고 검토로**("상품 정보 확인 필요"). `Draft.checks`로 `build_draft`에 들어간다.
- **편집 화면** `card_editor.py`: 상품 이름·가격·보조 설명, 일정 항목·내용 칸. 상품 수는 바꾸지 않는다(2~4개 유지). 상품 그림은 장별 배경과 같은 규칙(10MB, PNG·JPEG·WebP, 이름은 우리가)으로 `/review/{id}/slides/{i}/items/{j}/image`에 올리고 지운다. 사람이 올린 그림이라 "AI 생성 이미지" 표기와 무관하다.
- **렌더러**(서브): `_parts.html`의 `products`(2개 4:3, 3개 1:1, 4개 2:1 그림 칸 + 이름·보조·가격 알약)·`facts`(항목/내용 두 칸 줄) 매크로, `cards/editorial.html`, `base.css` 기본 모양 + 네 스타일별 덧칠. 그림이 없으면 자리표시 상자(이름만 크게). 상대 경로 그림은 배경과 같이 `resolve_asset`.
- **계약 문서** contracts.md 5절, 픽스처 `tests/fixtures/decks/editorial.json`, 동결 계약 테스트에 `editorial` 추가와 2개 미만·5개 이상 거절.

## 결과

- 사진 우선형을 쓰는 계정은 소식형 덱(표지 → 개요 → 상품 그리드 → 일정 패널 → 마무리)을 받는다. 가격·상품명이 자료에 없으면 사람이 확인한다.
- 테스트: 계약(모양·개수 상한·다른 템플릿에 섞기 금지), 네 스타일 × 상품 2·3·4개 × 보통/긴 글 PNG·넘침, 표기 슬롯·글자 HTML·그림 경로, 생성기 조립, 상품 검증, 편집·그림 올리기.
- 어려워진 것: 동결 계약이 바뀐다. 메인이 동의하지 않으면 이 PR은 닫고 S10·S11만 남긴다. 상품 그림은 사람이 올려야 한다(자동 확보는 후속).
- 후속: **밈 2단 장**(사진 두 장 + 검은 라벨 + 말풍선, 사용자 레퍼런스)은 장 종류가 하나 더 필요하다. 상품별 사진 자동 확보, 원고별 스타일(ADR 0037)은 별도.
