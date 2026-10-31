# 0056. 문서 근거 챗봇 1단계: 자체 포트, 제목 단위 조각, Gemini 임베딩, FLOAT[] 저장

- 상태: 제안 (배치 R1. 선웅제 요청: R1 청크·색인 → R2 검색 → R3 대시보드 챗봇)
- 날짜: 2026-10-30
- 관련: [ADR 0006](0006-single-process-duckdb-fastapi.md)(단일 프로세스·DuckDB), [ADR 0048](0048-gemini-classifier-replaces-jev.md)(Gemini 무료 구간), [ADR 0054](0054-error-taxonomy-and-evidence.md)(오류 종류 표)

## 배경

"프로젝트 문서를 근거로 답하는 챗봇"을 세 배치로 만든다. R1은 문서를 조각내고 임베딩을 붙여 저장하는 데까지다. 처음 자료는 docs의 ADR·devlog·incidents·user-guide·architecture·contracts·glossary이고, 나중에 수집 자료(DuckDB의 source_items)도 같은 구조에 붙인다.

## 결정

### 1. 포트를 `nsm/rag/ports.py`에 따로 둔다

`nsm/ports/`와 `nsm/domain/models.py`는 두 사람이 합의해야 바꾸는 동결 계약이다(contracts 1절). RAG는 게시·수집 흐름과 상관없는 새 기능이고, 아직 모양이 R2·R3를 거치며 바뀔 수 있다. 동결 계약에 넣으면 실험할 때마다 합의 PR이 필요하다. 그래서 `nsm/rag/` 안에 자체 Protocol 두 개를 둔다.
- `DocumentSource`: `kind`와 `documents()`. 첫 구현은 `DocsFolderSource`. 수집 자료는 같은 모양의 다른 출처로 붙이고, `doc_chunks.source_kind`로 출처마다 따로 증분·삭제한다.
- `Embedder`: `model`, `dim`, `embed(texts) -> vectors`. 첫 구현은 `GeminiEmbedder`, 테스트는 결정적 벡터를 내는 `tests.fakes.FakeEmbedder`.

모양이 R3까지 굳으면, 그때 동결 계약으로 올릴지 다시 본다.

### 2. 제목 구조대로 자르고, 경로를 본문 앞에 붙인다

- 제목마다 절을 나누고 조각은 절을 넘지 않는다. 조각마다 "파일명 > 상위 제목 > 소제목"을 저장하고 본문 첫 줄에 붙인다. 검색 결과 한 조각만 봐도 어디서 왔는지 알 수 있고, 임베딩에도 맥락이 들어간다.
- 목표 크기 500자, 겹침 80자(설정 `rag.chunk_chars`·`rag.chunk_overlap`). 300·500·800자로 실제 docs를 잘라 비교했다(devlog R1 표). 300자는 제목 단위의 43%가 쪼개져 한 절의 설명이 흩어지고, 800자는 대부분의 절이 이미 짧아 조각이 커지는 이득이 작았다. 500자는 쪼개진 비율이 23%다.
- 코드 블록은 중간에서 자르지 않는다(커도 통째로).
- 표도 행 중간에서 자르지 않는다. 다만 **목표보다 큰 표는 행 경계에서 나누고 조각마다 머리글을 다시 붙인다.** 처음 그대로 두니 architecture의 구현 상태 표 하나가 9,246자짜리 조각이 됐다. 그러면 "E2는 무엇을 했나" 같은 질문에 표 전체가 통째로 걸려 근거가 흐려지고, 임베딩 하나에 수십 행의 뜻이 뭉개진다. 행 단위로 나누면 조각마다 온전한 표(머리글 + 행)로 남는다. 이렇게 하니 가장 큰 조각이 2,869자(contracts의 파이썬 코드 블록)로 줄었다.
- 빈 양식(`adr/0000-template.md`, `incidents/0000-template.md`)과 `presentation/`, `learning/`, `samples/`는 제외한다(설정 `rag.exclude`). 발표·학습 자료는 개인 자료이고, 양식은 내용이 없다.

### 3. Gemini 임베딩을 REST로 직접

- 모델 `gemini-embedding-2`(2026-10-30 공식 문서의 최신 모델, 텍스트 입력 8,192토큰). 차원은 `outputDimensionality`로 768(설정 `rag.embedding_dim`). 공식 권장값은 768·1536·3072이고, 이 모델은 기본값이 아닌 차원도 자동으로 정규화한다.
- `batchEmbedContents`로 묶어 부른다. 한 번에 묶는 수는 100(설정 `rag.embed_batch_size`). 현재 문서에는 상한이 적혀 있지 않고, 예전 API가 100을 상한으로 뒀다.
- `gemini-embedding-2`는 `taskType`을 받지 않는다. 공식 권장대로 문서는 "title: {제목 경로} | text: {본문}" 형식으로 보낸다. 질문 쪽 형식("task: search result | query: …")은 R2에서 쓴다.
- **무료 구간 한도(실측).** 공식 한도 표에는 무료 구간이 더 이상 나오지 않는다(AI Studio에서 계정마다 본다). 그래서 실제로 걸려 보고 429 응답의 한도 이름을 읽었다.
  - `EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier` = 100, `EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier` = 1,000.
  - **묶음 하나가 아니라 묶음 안의 조각 하나하나를 1건으로 센다.** 100개 묶음이면 그 1분을 다 쓴다.
  - **막힌 시도도 하루 한도에 세어지는 것으로 보인다.** 저장된 조각은 230여 개뿐인데 하루 한도가 찼다. 처음 버전이 429마다 100개 묶음을 다시 보낸 탓이다.
  - 한도는 모델별이라 운영 분류기(`gemini-3.5-flash-lite`)의 한도와는 따로다.
- 그래서:
  - 묶음 100개 사이를 61초 띄운다(`rag.embed_interval_seconds`). **R3b에서 바뀜**: 고정 61초 대신 "직전 1분에 보낸 건수 + 이번 건수가 `rag.embed_per_minute`(100)를 넘을 때만, 넘지 않을 만큼" 기다린다. 고정 대기는 색인 뒤의 질문 1건까지 61초 기다리게 했다(2026-10-31 63초 사례). `embed_interval_seconds`는 예전 설정 파일 호환으로 받기만 하고 무시한다.
  - 분당 한도의 429는 응답의 대기 시간(RetryInfo)만큼 **한 번만** 기다렸다 다시 보낸다.
  - **하루 한도의 429는 다시 보내지 않고 바로 멈춘다**(`rate_limited`, `retry_at` = 응답의 대기 시간, 태평양 시간 자정에 풀림).
  - 실행 한 번에 임베딩하는 조각을 900개로 제한해(`rag.embed_max_per_run`) 하루 한도에 닿기 전에 스스로 멈춘다. docs 전체(1,181조각)는 무료 구간에서 이틀에 걸쳐 끝난다. 그다음부터는 바뀐 조각만 보낸다.
  - 색인은 멈춘 곳까지 저장하고, 다음 실행이 벡터 없는 조각부터 이어서 한다. 5xx·연결 오류는 `transient`, 응답 형식·차원이 다르면 `config_or_bug`.
- 분류기(`classifier/gemini.py`)처럼 httpx로 직접 부른다(SDK 의존 없음, 키는 헤더로만).
- Gemini 키가 없으면 조각만 저장하고 벡터는 NULL로 두고 그 사실을 알린다. 키를 넣고 다시 실행하면 벡터 없는 조각만 임베딩한다.

### 4. 비용은 `classifier` 종류, ref "embedding"

`CostKind`는 동결이라 `llm`·`image`·`classifier`뿐이다. 임베딩은 글을 만들지 않고 입력을 수치로 바꾸는 쪽이라 생성(`llm`)보다 판정(`classifier`)에 가깝고, 같은 Gemini 무료 구간을 쓰는 분류기와 한 줄로 묶여 보이는 편이 덜 헷갈린다. `llm`에 넣으면 원고 비용(Claude)과 섞여 원고 한 편 비용 계산이 흐려진다. ref에 "embedding"을 적어 분류기 비용과 구분하고, 대시보드 표시는 "자료 분류·문서 색인"으로 바꿨다. 다른 유료 경로처럼 하루 비용 상한에 닿았으면 임베딩하지 않는다. 금액은 응답의 `usageMetadata.promptTokenCount` × 설정 `rag.embedding_usd_per_mtok`이다. 무료 구간은 0원이라 기본값은 0이고, 유료(Standard)로 바꾸면 0.20달러/100만 토큰(2026-10-30 가격표)으로 바꾼다. 비용 표에 들어가므로 하루 비용 상한에 포함된다.

### 5. 저장: 같은 DuckDB의 `doc_chunks`, 벡터는 가변 길이 `FLOAT[]`

- 표: 출처 종류, 경로, 제목 경로, 순번, 본문(경로 줄 포함), 글자 수, 내용 해시(sha256), 임베딩 모델, 벡터, 만든 시각. 스키마 v6(`CREATE TABLE IF NOT EXISTS`, 기존 방식).
- **`FLOAT[]`(가변)를 골랐다.** `FLOAT[N]`은 차원이 표 정의에 박혀, 차원 설정을 바꾸면 표를 다시 만드는 마이그레이션이 필요하다. 그리고 모델이 바뀌는 동안 차원이 다른 벡터가 잠깐 섞일 수 있다. DuckDB 1.5.6은 `list_cosine_similarity`로 `FLOAT[]`끼리 바로 유사도를 계산한다(확인함). R2에서 HNSW 색인(vss 확장)이 필요해지면 그때 `embedding::FLOAT[768]`로 바꾼 뷰나 별도 열을 둔다. 지금 조각은 천여 개라 전수 비교로 충분하다.
- 증분: 내용 해시가 같고, 벡터가 있고(NOT NULL), 임베딩 모델과 **차원**이 같으면 예전 벡터를 그대로 쓴다(차원 설정을 바꾸면 다시 임베딩해 한 표에 차원이 섞이지 않게). 파일 전체가 그대로면 아예 다시 쓰지 않는다(두 번째 실행 0.4초). 파일이 사라지면 그 조각을 지운다. 다만 출처에서 문서를 하나도 못 찾았는데 저장된 조각이 있으면 경로 실수로 보고 지우지 않고 멈춘다. `--rebuild`는 전부 다시 한다.
- **벡터는 글자로 넘겨 SQL에서 `::FLOAT[]`로 바꾼다.** DuckDB에 파이썬 리스트(768개)를 매개변수로 넘기면 하나에 약 1초가 걸렸다(1,179조각이면 20분). 같은 값을 "[0.1,…]" 글자로 넘기면 행당 수 ms다(실측, devlog R1).

### 6. 명령은 개발·점검용, 로직은 함수로

`nsm index`(`--dry-run`, `--rebuild`, `--stats`, `--db`)는 `nsm/rag/command.run_index`를 부르고, 색인 자체는 `nsm/rag/indexer.index_documents(db, source, embedder, …)`다. R3에서 maintenance 잡이나 대시보드 버튼이 같은 함수를 부르면 `nsm run`을 끄지 않아도 된다. 지금은 `nsm run`이 DB를 쥐고 있으면 열 수 없으므로, 잠금 오류(Windows: "File is already open in …", 리눅스·맥: "Could not set lock … Conflicting lock")를 알아보고 원인과 해결법을 한국어로 안내한다. 스케줄러에는 아직 넣지 않는다.

## 결과

- docs 130개 파일이 조각 1,100여 개가 된다(실측은 devlog R1).
- 어려워진 것: 큰 표를 행으로 나눠 "표를 통째로"라는 단순 규칙에서 벗어났다. 대신 조각마다 머리글이 붙어 의미는 유지된다. 무료 구간 한도는 문서로 확정할 수 없어 간격·재시도를 보수적으로 잡았다. 실제로 막히면 다음 실행이 이어서 한다.
