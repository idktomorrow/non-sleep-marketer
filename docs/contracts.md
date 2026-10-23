# 병렬 작업 계약 (contracts)

> 작성일: 2026-10-11 · 상태: 1일차에 코드로 동결(이슈 M1) · 관련: [PRD](PRD.md) · [issues.md](issues.md)
> 2026-10-11 변경: S1(#17)·S4(#20)·S6(#22)는 구현과 같은 PR에서 통과시키기 위해 메인 담당으로 옮김. 서브는 `nsm/render/`, `nsm/templates/`, `tests/render/`, `tests/contracts/`, `tests/fixtures/decks/` 담당.
> 2026-10-14 변경: 2주차 서브 배치 S-β(#21)·S-γ(#23)·S8(#34)을 메인이 맡음([ADR 0034](adr/0034-main-takes-s-beta-gamma-s8.md)). 세 배치 동안 `tests/contracts/`, `nsm/render/`, `nsm/templates/`, `tests/render/`는 메인 소유, 머지 후 서브가 다듬을 수 있음.
> 이 문서는 메인(선웅제)과 서브 팀원이 서로 기다리지 않고 동시에 작업하기 위한 **약속**입니다. 바꾸려면 PR로 두 사람이 합의합니다.

## 1. 디렉터리와 소유권

```
non-sleep-marketer/
  nsm/
    cli.py                # nsm init / run / run --once / render-preview / install-autostart
    config/               # YAML 스키마, 로딩, 설정 마법사, keyring            [메인]
    domain/               # 도메인 모델(pydantic), 정책 규칙(순수 함수), 상태 머신  [메인]
    ports/                # Protocol 인터페이스 (아래 3절)                      [메인, 동결]
    adapters/
      platforms/          # instagram.py threads.py dryrun.py (naver_blog는 범위 밖, ADR 0016) [메인]
      drivers/            # aside.py playwright.py  (브라우저 드라이버)           [메인]
      sources/            # upload.py naver.py google_trends.py community.py     [메인]
      classifier/         # gemini.py claude.py keyword.py (jev는 C27에서 제거)   [메인]
      generator/          # claude_text.py image_api.py                          [메인]
      notify/             # windows.py (winotify), log.py                        [메인]
    render/               # 카드 덱 JSON → PNG (Playwright)                      [서브]
    templates/            # cards/{info,list,compare}.html, base.css, fonts/     [서브]
    engine/               # jobs: collect, generate, publish, reply, metrics, health, weekly_report [메인]
    store/                # DuckDB 스키마, 마이그레이션, 리포지토리              [메인]
    app/                  # FastAPI 대시보드 (routes/, templates/, static/)       [메인]
    scheduler/            # APScheduler 부트, 단일 프로세스 실행                   [메인]
  tests/
    conftest.py           # 메모리 DuckDB, 가짜 시계, 픽스처 로더                [메인]
    fakes/                # 가짜 포트 구현 (아래 7절)                            [메인]
    fixtures/             # 시나리오 픽스처 (아래 6절)  [메인, decks/는 서브]
    engine/               # 경계 1: 잡 단위 테스트                               [메인]
    contracts/            # 경계 2: 어댑터 계약 테스트 (@pytest.mark.contract)   [서브]
    render/               # 경계 3: 템플릿 렌더 스냅샷 테스트                     [서브]
    app/                  # 대시보드 라우트 테스트 (FastAPI TestClient)           [메인]
  docs/
  .github/workflows/ci.yml                                                   [메인]
  pyproject.toml          # uv, ruff, pytest 설정                               [메인 생성, 서브는 pytest 섹션 수정 가능]
```

규칙
- 자기 소유 디렉터리는 자유롭게 수정. 남의 디렉터리는 PR + 리뷰 요청.
- `nsm/ports/`, `nsm/domain/models.py`, 이 문서의 JSON 계약은 **동결**. 변경은 둘이 합의 후 같은 PR에서 호출부까지 수정.
- 브랜치: 이슈당 `feat/M3-persona-wizard` 같은 브랜치 → `main`에 PR. 하루에 한 번 이상 `main`을 가져옴.
- UI/UX 디자인(`nsm/app/templates`, `static`)은 메인이 마지막 주에 작업. 그 전까지 대시보드 HTML은 "동작 확인용 최소"로 둠.

## 2. 도메인 모델 (`nsm/domain/models.py`, pydantic)

> 배치 A 이후 **코드가 원본**입니다. 아래는 초안 요약이며 차이가 있으면 `nsm/domain/models.py`가 우선합니다. 주요 차이: 게시 상태를 플랫폼별 `Publication`으로 분리([ADR 0009](adr/0009-per-platform-publication-state.md)), `GeneratedContent.blog_title` 추가, `ImageGenerator`는 이미지가 없으면 `None` 반환, 비용 보고용 `CostSink` 포트 추가.

```python
Platform = Literal[
    "instagram", "threads", "naver_blog"
]  # naver_blog는 동결 값으로만 남음(ADR 0016)
ContentStatus = Literal[
    "draft",
    "pending_review",
    "approved",
    "publishing",
    "published",
    "failed",
    "abandoned",
    "discarded",
]
CommentCategory = Literal[
    "praise_general", "question", "complaint", "price_refund", "legal", "spam"
]
CommentAction = Literal["auto_reply", "hide", "escalate", "ignore"]
TemplateKind = Literal["info", "list", "compare"]


class Persona(BaseModel):
    name: str
    handle: str
    character: str
    tone: str
    banned_words: list[str]
    banned_patterns: list[str]  # 정규식, 예: 1인칭 체험 표현
    topics: list[str]
    active_hours: list[tuple[time, time]]  # 로컬 시간대 구간
    enabled: bool
    ad_label: bool  # "광고" 표기 삽입 여부


class Brand(BaseModel):
    name: str
    industry: str
    products: list[str]
    key_messages: list[str]
    primary_color: str
    accent_color: str
    logo_path: Path | None


class SourceItem(BaseModel):
    id: str
    source: str
    url: str | None
    title: str
    text: str
    published_at: datetime | None
    collected_at: datetime
    url_hash: str


class Classification(BaseModel):
    label: str
    confidence: float  # 0.0 ~ 1.0


class Claim(BaseModel):
    text: str
    source_item_id: str  # 모든 사실 주장은 출처 자료와 연결


class GeneratedContent(BaseModel):
    topic: str
    template: TemplateKind
    deck: CardDeck  # 5절
    threads_text: str
    blog_markdown: str
    claims: list[Claim]
    instagram_caption: str


class Content(BaseModel):  # DB 레코드
    id: str
    status: ContentStatus
    generated: GeneratedContent
    relevance_confidence: float
    created_at: datetime
    review_reason: str | None
    attempts: int
    next_attempt_at: datetime | None


class RenderedPost(BaseModel):  # 어댑터에 넘기는 플랫폼별 최종 형태
    content_id: str
    platform: Platform
    text: str
    images: list[Path]
    title: str | None  # 블로그는 title 필수


class PublishResult(BaseModel):
    external_id: str
    url: str


class Comment(BaseModel):
    platform: Platform
    external_id: str
    post_external_id: str
    author_id: str
    author_name: str
    text: str
    created_at: datetime


class PostMetrics(BaseModel):
    platform: Platform
    post_external_id: str
    captured_at: datetime
    views: int | None
    reach: int | None
    impressions: int | None
    likes: int | None
    comments: int | None
    saves: int | None
    shares: int | None


class AccountMetrics(BaseModel):
    platform: Platform
    captured_at: datetime
    followers: int | None


class HealthStatus(BaseModel):
    logged_in: bool
    detail: str


class CostRecord(BaseModel):
    kind: Literal["llm", "image", "classifier"]
    usd: float
    at: datetime
    ref: str
```

## 3. 포트 (`nsm/ports/__init__.py`, `typing.Protocol`)

> 배치 A 이후 코드가 원본입니다.

```python
class PlatformAdapter(Protocol):
    platform: Platform

    def capabilities(self) -> set[str]:
        ...
        # 가능한 값: "publish" "fetch_comments" "reply" "hide_comment" "delete_comment" "post_metrics" "account_metrics"

    def health(self) -> HealthStatus: ...
    def publish(self, post: RenderedPost) -> PublishResult: ...
    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]: ...
    def reply(self, comment: Comment, text: str) -> str: ...  # 답글 external_id
    def hide_comment(self, comment: Comment) -> None: ...  # capability 없으면 NotSupported
    def read_post_metrics(self, post_external_id: str) -> PostMetrics: ...
    def read_account_metrics(self) -> AccountMetrics: ...


class Source(Protocol):
    name: str

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]: ...


class Classifier(Protocol):
    def classify(self, text: str, labels: list[str], instruction: str) -> Classification: ...
    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]: ...


class TextGenerator(Protocol):
    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent: ...
    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str: ...


class ImageGenerator(Protocol):
    def generate_background(
        self, prompt: str, width: int, height: int
    ) -> Path: ...  # 비활성 시 NullImageGenerator


class CardRenderer(Protocol):  # [서브 구현]
    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]: ...  # 슬라이드 순서대로 PNG


# S13 제안(합의 필요, ADR 0054): 범용 이미지 제작기와 전역 비용 원장. ImageGenerator·CostSink는 그대로.
class ImageStudio(Protocol):
    def make(
        self, spec: ImageSpec
    ) -> ImageResult: ...  # 참조 이미지·투명 배경·크기·품질. 글자는 넣지 않음


class ScenarioWriter(Protocol):  # S14 제안(ADR 0056): 밈 대본. 정보 원고(TextGenerator)와 별도 경로
    def propose_meme_ideas(self, req: MemeRequest) -> list[MemeIdea]: ...
    def write_meme_script(self, req: MemeRequest, idea: MemeIdea) -> MemeScript: ...


class CostLedger(CostSink, Protocol):
    def reserve(self, kind, provider, estimate_usd, ref="", model="") -> str: ...
    def confirm(
        self, reservation_id, usd, status="confirmed", detail=None, request_id=""
    ) -> None: ...
    def release(self, reservation_id) -> None: ...
    def mark_unknown(self, reservation_id, request_id="") -> None: ...
    def spent_between(self, start: datetime, end: datetime) -> float: ...


class Notifier(Protocol):
    def notify(self, title: str, body: str, level: Literal["info", "warn", "error"]) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...  # 테스트에서 고정 시각 주입
```

브라우저 드라이버는 어댑터 내부 세부사항이며 포트가 아닙니다. `adapters/drivers/`는 `open(url)`, `click`, `type`, `upload`, `read_text` 수준의 얇은 래퍼이고 Aside/Playwright 두 구현을 둡니다. 플랫폼 어댑터는 드라이버를 주입받습니다.

## 4. 엔진 잡과 정책 (`nsm/engine/`, `nsm/domain/policy.py`)

| 잡 | 기본 주기 | 하는 일 |
|---|---|---|
| `health` | 30분 | 각 어댑터 `health()`. 로그아웃이면 알림 + 그 플랫폼 publish/reply 중단 |
| `collect` | 하루 2회, 활동 시간 내, ±30분 지터 | 소스별 `collect` → URL 해시 중복 제거 → 관련성 분류(0.8 이상만 유지) → 주제 분류 → 저장 |
| `generate` | 하루 1회 | 내일 쿼터만큼 주제 선정(최근 14일 주제 제외) → 생성 → 금칙어·표기·클레임 검사 → `pending_review` 또는 `approved` |
| `publish` | 게시 슬롯(플랫폼별 하루 N회, 활동 시간 내 무작위 시각) | `approved` 1건 → `publishing` 기록 → 렌더 → 어댑터 `publish` → `published` + URL 저장. 실패 시 `failed`, 재시도 3회(15분·1시간·4시간) 후 `abandoned` |
| `reply` | 10분, ±3분 지터, 활동 시간 내 | 최근 14일 게시물의 새 댓글 → 6분류 → 정책 → 답글/숨김/에스컬레이션. 동일 작성자 하루 3회 제한 |
| `metrics` | 하루 1회 | 최근 7일 게시물 지표 + 계정 팔로워 스냅샷 |
| `weekly_report` | 주 1회 | 최고 성과 콘텐츠와 이유, 다음 주 추천 주제, 비용 합계 (2주차) |

모든 잡은 `run_once(ctx)` 하나로 호출되고 `ctx`에 포트·저장소·시계·설정이 들어갑니다. 테스트는 이 `run_once`를 호출합니다.

정책(순수 함수, 입력 → 결정)
- `decide_publish(content, settings) -> ("approved" | "pending_review", reason)`
  - 보호 모드(첫 20건 또는 사용자가 자동 게시 미허용) → 검토
  - claims 중 source_item_id가 DB에 없는 것이 있으면 → 검토
  - relevance_confidence < 0.8 → 검토
  - 금칙어·금칙 패턴 포함 → 검토(사유 기록)
  - 그 외 → 승인
- `decide_comment(category, author_daily_count, capabilities) -> CommentAction`
  - praise_general, question → auto_reply (단 하루 3회 초과 시 ignore)
  - spam → hide (capability 없으면 escalate, 사유 "스팸 추정")
  - complaint, price_refund, legal → escalate + 알림
- `in_active_hours(now, persona) -> bool`. `persona.enabled`가 False면 collect/generate/publish/reply 모두 건너뜀(health, metrics는 계속)
- `apply_labels(text, platform, persona) -> str`: 첫 줄에 표기 삽입(5절 표기 규칙)
- `check_quota(platform, today_count, settings) -> bool`, `check_cost_cap(today_usd, settings) -> bool`

기본 설정값: 게시 대상 instagram·threads, 쿼터 instagram 1 / threads 2 (일), 비용 상한 3달러/일, 보호 모드 20건, 재시도 3회.

재시작 복구: 시작 시 `publishing` 상태로 남은 콘텐츠는 자동 재시도하지 않고 `pending_review`로 옮기고 사유 "게시 중 중단됨, 플랫폼 확인 필요"를 기록합니다(이중 게시 방지).

## 5. 카드 덱 JSON 계약 (`CardDeck`) — 서브가 1일차부터 작업하는 기준

```json
{
  "template": "info",
  "brand": { "name": "브랜드명", "primary_color": "#1E40AF", "accent_color": "#F59E0B", "logo_path": null },
  "persona": { "name": "루나", "handle": "@luna.ai" },
  "labels": { "virtual_person": true, "ad": false, "ai_generated_image": true },
  "slides": [
    { "kind": "cover", "title": "제목 (최대 2줄, 줄당 14자 내외)", "subtitle": "부제 (1줄)", "background": "C:/.../bg1.png" },
    { "kind": "body",  "heading": "소제목", "body": "본문 (최대 90자)", "bullets": [], "background": null },
    { "kind": "list",  "heading": "소제목", "bullets": ["항목 1", "항목 2", "항목 3"], "background": null },
    { "kind": "compare", "heading": "A vs B", "left": { "title": "A", "points": ["..."] }, "right": { "title": "B", "points": ["..."] } },
    { "kind": "products", "heading": "라인업", "items": [{ "name": "머그컵", "price": "¥5,500", "note": "전 3종", "image": null }, { "name": "키링", "price": "", "note": "", "image": "C:/.../item.png" }], "background": null },
    { "kind": "facts", "heading": "구매처 & 일정", "rows": [{ "label": "출시", "value": "10월 23일(금)부터" }, { "label": "구매 수량", "value": "1인 2개" }], "background": null },
    { "kind": "outro", "cta": "저장하고 나중에 보기", "sources": ["https://example.com/a", "https://example.com/b"] }
  ]
}
```

- 크기: **1080×1350** (4:5). 슬라이드 수 2~10장(인스타그램 캐러셀 상한). 같은 PNG를 스레드와 블로그에도 씀.
- 템플릿 3종: `info`(cover + body… + outro), `list`(cover + list… + outro), `compare`(cover + compare + outro). 템플릿은 `kind`별 슬라이드 레이아웃을 가져야 함.
- **S14 제안(ADR 0055, 합의 전 미적용)**: `ImageAsset{path, ai, alt}`; `comic` 장(`panels` 정확히 2개: `image` ImageAsset|null, `label` ≤30자, `bubble` ≤40자, `bubble_side`, `scene`); body·products·facts에 `sticker` ImageAsset|null; 템플릿 `meme`(첫 장 cover **또는** comic, 중간 comic·body, 마지막 outro), `editorial` 중간에 comic 허용. `labels.ai_slides: list[int] | null` — null이면 레거시 규칙(`ai_generated_image` + background), 목록이면 그 장에만 "AI 생성 이미지"(빈 목록 = 없음). 기존 JSON은 그대로 읽힌다.
- **S12 제안(ADR 0052, 합의 전 미적용)**: 템플릿 `editorial`(cover + body·products·facts 섞어서 + outro). `products`는 항목 2~4개(`name`, `price` 표기 그대로, `note`, `image` 로컬 경로 또는 null), `facts`는 2~6줄(`label`, `value`). 기존 세 템플릿·기존 JSON은 그대로 읽힌다. 상품 이름·가격이 자료에 없으면 자동 승인하지 않는다.
- **S18 제안(ADR 0062·0063, 합의 전 미적용)**: `ImageAsset.source`(사진을 가져온 기사 주소); body·list·compare·products·facts에 `photo` ImageAsset|null·`photo_hint`; `FactsSlide.layout`(`rows`|`timeline`|`conditions`|`figures`, 생성기가 데이터로 검증); `CardDeck.sources`(덱 출처, 마무리 장이 없어도 남음); `editorial`은 가운데에 list·compare도 허용하고 outro는 선택. 포트 `MemeFormatPlan.make`(`composite`|`restage`, 기본 restage). 기존 JSON은 그대로 읽힌다.
- 폰트: Pretendard(SIL OFL)를 `nsm/templates/fonts/`에 동봉. 한글은 항상 HTML 텍스트로 그림(AI 이미지에 글자 넣지 않음).
- `background`가 null이면 브랜드 색 기반 그라디언트/패턴으로 대체(이미지 생성을 끈 경우에도 완성도 유지).
- 스타일(S8, ADR 0036 · S10 제안, ADR 0050 · S18 제안, ADR 0063): `clean`·`photo`·`illust`·`editorial`·`kitsch`(소식형 구성 + 장식 CSS, 표기 슬롯 같음). 덱 JSON이 아니라 설정 `brand.card_style`(렌더러 옵션)로 고르므로 이 계약은 그대로다. 스타일은 장식(코드로 그린 배경·캐릭터)만 바꾸고 텍스트·표기 슬롯은 같다. 코드로 그린 배경은 `background`가 아니므로 `ai-generated` 캡션 대상이 아니다. 원고별 스타일(덱 `style` 필드)은 2차 계약 변경 제안(ADR 0037).
- **표기 슬롯(필수)**: 아래 요소가 DOM에 존재해야 하고 렌더 테스트가 이를 검사함.
  - `[data-label="virtual-person"]`: 텍스트 "가상인물" 배지. `labels.virtual_person`이 true면 페르소나 이름 바로 옆에 배경과 대비되는 색으로 표시. 이름(인물)과 배지는 cover·outro 슬라이드에만 함께 두고, 가운데 슬라이드에는 둘 다 두지 않음(ADR 0043, 그 전에는 모든 슬라이드). 덱 JSON은 그대로.
  - `[data-label="ai-generated"]`: "AI 생성 이미지" 작은 캡션. `labels.ai_generated_image`가 true일 때 배경 이미지가 있는 슬라이드에 표시.
    - 일러스트형에서 AI로 만든 캐릭터 그림(설정 `brand.character_ai_made`)이 들어간 표지·마지막 장에도 같은 슬롯으로 표시한다(C17, ADR 0046).
    - 캐릭터는 덱 JSON이 아니라 렌더러 옵션(설정 `brand.character`)이라 덱 계약은 그대로다. 팀원 확인 필요.
  - `[data-label="ad"]`: "광고" 배지. `labels.ad`가 true면 cover 슬라이드 상단에 크게 표시.
  - `[data-sources]`: outro 슬라이드의 출처 목록.
  - `[data-page]`: "n / N" 페이지 표시.
  - **S18 제안(사용자 결정 2026-10-23, 합의 전)**: 카드에서 페르소나 이름·`virtual-person` 배지·`[data-page]`·`[data-sources]`를 뺀다. 가상인물 표기는 게시 글 첫 줄 문구(아래)로만 하고, 출처는 게시 글 끝 "출처" 블록에 실제 주소로 붙인다. `ad`는 카드에 남는다. `ai-generated`도 카드에서 빼고 게시 글 둘째 줄 "AI 생성 이미지가 포함된 게시물입니다"로 옮긴다(같은 날 사용자 결정, 기준은 `labels.ai_slides` 그대로). 이미지 안 "가상인물과 근접한 위치" 표기가 필요한지 메인과 법규 근거(ADR 0019)를 다시 확인해야 하며, 합의 전에는 이 PR을 머지하지 않는다.
- 텍스트 표기 규칙(생성기·정책이 적용, 템플릿 밖):
  - 인스타그램 캡션·스레드 본문 **첫 줄**: `인공지능(AI)을 기반으로 생성된 가상인물이 포함된 게시물입니다` (공정위 예규 제499호 Ⅴ.5 예시 문구 그대로, ADR 0019). 광고면 그 앞에 `#광고 ` 추가(첫 부분이자 첫 번째 해시태그).
  - 카드의 `virtual-person` 배지는 페르소나 이름 바로 옆("가상인물과 근접한 위치").
  - 블로그 제목 끝에 ` [가상인물 포함]`(네이버 블로그는 범위 밖, ADR 0016).
  - 이 문구들은 상수 `nsm/domain/labels.py`에 둔다. 배치 B(2026-10-13)에서 원문과 대조해 확정(docs/day1-verification.md).

렌더 하네스: `nsm render-preview path/to/deck.json --out ./preview/` → PNG 생성. 서브는 `tests/fixtures/decks/*.json`에 템플릿별 샘플 덱을 두고, 디자인 중 이 명령으로 확인합니다.

## 6. 픽스처 규격 (`tests/fixtures/`)

- `decks/{info,list,compare}.json`: 5절 계약을 따르는 샘플 덱 (배치 A에서 메인이 초안, 서브가 S-α에서 다듬음. 렌더·엔진 테스트 공용).
- `sources/*.json`: `SourceItem` 배열. 가짜 소스가 돌려줌.
- `comments/*.json`: `Comment` 배열 + 기대 분류. 예:

```json
[{ "comment": { "platform": "threads", "external_id": "c1", "post_external_id": "p1",
                "author_id": "u1", "author_name": "민수", "text": "환불 되나요?",
                "created_at": "2026-10-11T10:00:00+09:00" },
   "expected_category": "price_refund" }]
```

- `metrics/*.json`: `PostMetrics`/`AccountMetrics` 배열.
- `personas/*.yaml`: 설정 마법사가 만드는 것과 같은 YAML(스키마는 `nsm/config/schema.py`, 메인이 1일차에 동결).
- **드라이런 어댑터**(메인 구현)는 `comments/`, `metrics/` 픽스처를 읽어 가짜 댓글·지표를 돌려주므로, 서브가 만든 픽스처가 곧 드라이런 시뮬레이션 데이터입니다.

## 7. 가짜 포트 (`tests/fakes/`, 메인 구현)

- `FakePlatformAdapter`: 호출 기록(`published`, `replied`, `hidden`), 설정 가능한 실패(`fail_next_publish`), capability 집합 주입.
- `FakeSource`: 픽스처 반환. `FakeClassifier`: 텍스트→라벨 매핑 테이블 또는 고정 신뢰도. `FakeTextGenerator`: 픽스처 덱 + claims 반환(출처 누락 시나리오 지원). `NullImageGenerator`. `FakeRenderer`: 빈 PNG 파일 생성. `FakeNotifier`: 호출 기록. `FixedClock`.
- 드라이런 어댑터(제품 기능, 메인)와 가짜 어댑터(테스트 전용, 서브)는 다릅니다. 드라이런은 미리보기 폴더에 파일을 쓰고, 가짜는 메모리에 기록만 합니다.

## 8. DB 테이블 (`nsm/store/schema.sql`, DuckDB)

> 배치 A 이후 `schema.sql`이 원본입니다. 시각은 UTC naive로 저장합니다.

`source_items`, `classifications`, `contents`(status, generated JSON, attempts, next_attempt_at, review_reason), `publications`(content_id, platform, external_id, url, published_at), `comments`(+ category, action, handled_at, handled_by), `reply_counts`(platform, author_id, date, count), `metrics_posts`, `metrics_account`, `costs`, `run_logs`(job, started_at, finished_at, ok, message), `settings_kv`(persona_enabled, auto_publish_enabled, protect_count 등 대시보드에서 바꾸는 값).

- 단일 프로세스 내 스레드(스케줄러·FastAPI)는 하나의 `duckdb.connect()`에서 `cursor()`를 스레드별로 생성해 사용.
- 테스트는 `duckdb.connect(":memory:")`에 같은 `schema.sql` 적용.

## 9. CLI (`nsm/cli.py`)

| 명령 | 동작 |
|---|---|
| `nsm init` | 설정 마법사(위험 고지 동의 → 브랜드 → 페르소나 → API 키(keyring) → 플랫폼 연결은 나중에 가능) |
| `nsm run` | 단일 프로세스 시작: 스케줄러 + 대시보드(http://127.0.0.1:8765) |
| `nsm run --once --job collect` | 특정 잡 1회 실행 (디버그·E2E용) |
| `nsm render-preview deck.json --out DIR [--style clean\|photo\|illust\|editorial]` | 덱 → PNG (스타일 기본: 설정의 `brand.card_style`) |
| `nsm install-autostart` / `uninstall-autostart` | Windows 작업 스케줄러에 로그온 시 `nsm run` 등록 |
| `nsm doctor` | 환경 점검(API 키, 드라이버 로그인 상태, 폰트) |

전역 설정 `dry_run: true`면 모든 플랫폼 어댑터가 드라이런 구현으로 바뀌고 산출물은 `./preview/YYYY-MM-DD/<content_id>/`에 PNG·캡션·스레드 글·블로그 md로 저장됩니다.

## 10. 테스트 규약

- 경계 1(잡 단위): `tests/engine/test_<job>_*.py`. 가짜 포트 + 메모리 DuckDB + FixedClock. 기본 `pytest`에 포함, 전체 30초 내.
- 경계 2(어댑터 계약): `tests/contracts/test_platform_contract.py`가 어댑터 픽스처를 파라미터로 받아 동일 시나리오 실행. 드라이런 어댑터는 기본 실행에 포함, 실계정은 `-m contract`로만.
- 경계 3(렌더): `tests/render/`. 템플릿별 샘플 덱 → PNG 존재·크기 1080×1350·표기 슬롯 DOM 존재. 기본 실행 포함(Playwright 크로미움 필요, CI에 설치).
- 대시보드: `tests/app/` 라우트 테스트(승인 → 상태 변화 등). 화면 모양은 테스트하지 않음.
- 플랫폼별 지표 가용성(계약 테스트가 검사하는 필드):

| 지표 | instagram(프로 계정) | threads | naver_blog(범위 밖, 참고) |
|---|---|---|---|
| views/impressions, reach | ○ | views ○ | 조회수 ○ |
| likes, comments | ○ | ○ | 공감·댓글 ○ |
| saves, shares | ○ | reposts·quotes → shares | × |
| followers | ○ | ○ | 이웃 수 |
