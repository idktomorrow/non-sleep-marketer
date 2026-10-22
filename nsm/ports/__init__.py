"""포트: 엔진이 바깥 세계와 대화하는 인터페이스. docs/contracts.md 3절의 코드 원본 (동결).

엔진·정책은 이 Protocol만 알고, 구현(어댑터)은 nsm/adapters/ 아래에 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from nsm.domain.models import (
    AccountMetrics,
    Brand,
    CardDeck,
    Classification,
    Comment,
    ContentBrief,
    CostKind,
    GeneratedContent,
    HealthStatus,
    Persona,
    Platform,
    PostMetrics,
    PublishResult,
    RenderedPost,
    SourceItem,
)

CAPABILITIES = frozenset(
    {
        "publish",
        "fetch_comments",
        "reply",
        "hide_comment",
        "post_metrics",
        "account_metrics",
    }
)


class NotSupportedError(Exception):
    """어댑터가 capability를 선언하지 않은 동작을 호출받았을 때."""


class AdapterError(Exception):
    """외부 동작 실패(네트워크, 화면 변경, 로그인 만료). 재시도 대상."""


@runtime_checkable
class PlatformAdapter(Protocol):
    platform: Platform

    def capabilities(self) -> set[str]: ...
    def health(self) -> HealthStatus: ...
    def publish(self, post: RenderedPost) -> PublishResult: ...
    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]: ...
    def reply(self, comment: Comment, text: str) -> str: ...
    def hide_comment(self, comment: Comment) -> None: ...
    def read_post_metrics(self, post_external_id: str) -> PostMetrics: ...
    def read_account_metrics(self) -> AccountMetrics: ...


@runtime_checkable
class Source(Protocol):
    name: str

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]: ...


@runtime_checkable
class Classifier(Protocol):
    def classify(self, text: str, labels: list[str], instruction: str) -> Classification: ...
    def classify_many(
        self, texts: list[str], labels: list[str], instruction: str
    ) -> list[Classification]: ...


@runtime_checkable
class TextGenerator(Protocol):
    def generate_content(
        self, brief: ContentBrief, persona: Persona, brand: Brand
    ) -> GeneratedContent: ...
    def generate_reply(self, comment: Comment, persona: Persona, post_context: str) -> str: ...


@runtime_checkable
class ImageGenerator(Protocol):
    def generate_background(self, prompt: str, width: int, height: int) -> Path | None: ...


# ---- S13 제안(동결 계약 추가, 합의 필요): 범용 이미지 제작기와 비용 원장 ------------------------------------
# ImageGenerator(표지 배경 한 장)와 별개로, 역할·참조 이미지·투명 배경을 지정해 그림을 만드는 포트.
# 글자는 절대 그림에 넣지 않는다(ADR 0007). 엔진은 ImageSpec을 만들고 결과 파일만 받는다.


@dataclass(frozen=True)
class ImageSpec:
    prompt: str
    width: int = 1024
    height: int = 1024
    quality: Literal["low", "medium", "high"] | None = (
        None  # None이면 제작기의 설정값(models.image_quality)
    )
    transparent: bool = False  # 투명 배경(스티커). png로 받는다
    references: tuple[Path, ...] = ()  # 참조 이미지(캐릭터 외형 유지용). 있으면 편집 엔드포인트
    negative: str = ""  # 제외할 요소(문장으로 프롬프트 끝에 붙는다)
    ref: str = ""  # 비용·로그에 남길 이름(예: "sticker:s1")


@dataclass(frozen=True)
class ImageResult:
    path: Path
    model: str
    usd: float
    cost_status: Literal["confirmed", "estimated", "unknown"]
    request_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class ImageRefused(Exception):
    """정책 차단(moderation_blocked)처럼 다시 보내도 같은 결과인 실패. 사유를 사람에게 보여 준다."""

    def __init__(self, message: str, categories: tuple[str, ...] = (), stage: str = "") -> None:
        super().__init__(message)
        self.categories = categories
        self.stage = stage


class ImageUnknown(Exception):
    """요청이 서버에 닿은 뒤 응답을 못 받음(타임아웃·끊김). 생성·과금 여부가 불명확하므로 자동 재시도 금지."""


@runtime_checkable
class ImageStudio(Protocol):
    def make(self, spec: ImageSpec) -> ImageResult: ...


# ---- S14 제안(동결 계약 추가, 합의 필요, ADR 0056): 밈 대본 작가. 정보 원고(TextGenerator)와 별도 경로다.


@dataclass(frozen=True)
class MemeExample:
    """밈 탐색에서 찾은 실제 사례(S16): 구조를 참고할 뿐 복제하지 않는다."""

    title: str
    snippet: str
    url: str
    platform: str
    category: str = "C"  # A 최근 확산 확인 | B 지속 형식 | C 근거 부족 발견


@dataclass(frozen=True)
class MemeRequest:
    topics: tuple[str, ...]  # 이 밈이 다룰 관심 주제(한 게시물에 주제 하나)
    character_name: str  # 비면 주인공 미정: 대본이 주인공 후보를 제안만 한다(그림은 선택 뒤)
    character_description: str  # 선택한 캐릭터의 외형·성격(프로필). 페르소나 인물상을 쓰지 않는다
    tone: str  # 밈 말투(호출별, 전역 페르소나를 덮어쓰지 않는다)
    recent: tuple[str, ...] = ()  # 최근 밈 요약("상황 → 반전")들: 같은 농담을 피하게
    hook: str = ""  # 이 주제의 최근 소식 한 줄. 비우면 뉴스 없이 그 분야의 공감 상황극
    hook_sources: tuple[SourceItem, ...] = ()  # 훅의 자료(있으면 claims·출처 허용)
    count: int = 3  # 아이디어 수
    examples: tuple[MemeExample, ...] = ()  # 밈 탐색 사례(구조 참고용)
    caution: str = ""  # 주제별 주의(예: 실제 인물의 하지 않은 행동·발언을 사실처럼 쓰지 않음)


@dataclass(frozen=True)
class MemeIdea:
    situation: str  # 공감 상황 한 줄("극중 월급 vs 현실 연봉")
    contrast: str  # 두 컷의 대비("시무룩 vs 느긋")
    punchline: str  # 반전 한 줄
    why: str = ""  # 왜 웃긴지(검토자용)
    based_on: tuple[int, ...] = ()  # 참고한 탐색 사례 번호(없으면 창작)
    same_as_recent: int = -1  # 최근 밈 목록 중 의미상 같은 농담의 번호(-1이면 없음, 모델 판단)


@dataclass(frozen=True)
class MemePanel:
    label: str  # 검은 라벨(≤30자)
    bubble: str  # 말풍선(≤40자)
    scene: str  # 장면 지시(표정·행동·배경, 그림 생성용)
    bubble_side: Literal["left", "right"] = "right"


@dataclass(frozen=True)
class MemeScript:
    idea: MemeIdea
    panels: tuple[MemePanel, MemePanel]
    caption: str
    threads_text: str
    cta: str = ""
    claims: tuple[tuple[str, str], ...] = ()  # (문장, 자료 id). 뉴스 훅이 있을 때만
    suggested_character: str = ""  # 주인공 미정일 때 대본이 제안한 주인공(예: 작품 캐릭터 이름)


# ---- S17 제안(ADR 0061): 유명·유행 밈 형식을 찾아 구도·연출·레이아웃까지 재해석한다(2컷 고정이 아님)


@dataclass(frozen=True)
class MemeFormatCandidate:
    """탐색한 밈 형식 하나. 그림은 **밈 참조**(구도·행동·표정 대비·텍스트 위치 이해용)이지 캐릭터 참조가 아니다."""

    key: str  # 안정된 이름(예: "this-is-fine")
    title: str
    url: str  # 확인한 출처 페이지
    image_path: str = ""  # 대표 사례 그림(로컬 사본). 없으면 ""
    image_url: str = ""
    # "recent"(페이지에 최근 날짜의 확산 기록) | "listed"(인기 목록에 있지만 최근 기록 없음) | "classic"(오래됐지만 유명한 형식)
    # | "local"(국내 해설 사이트 '유행 중' 표시) | "fan"(팬 커뮤니티 사례, 근거 부족) | "original"(새로 만드는 창작). S18
    era: str = ""
    evidence: str = ""  # 확인 가능한 확산 근거(페이지에 적힌 날짜·수치 그대로, 추정 없음)
    about: str = ""  # 형식 설명(페이지 요약)
    site: str = ""  # 출처 사이트 이름
    checked_at: str = ""
    latest_evidence: str = ""  # 페이지에 적힌 가장 최근 확산 날짜(YYYY-MM-DD), 없으면 ""(S18)


@dataclass(frozen=True)
class MemeFormatRequest:
    topic: str
    character_name: str
    character_description: str
    tone: str
    candidates: tuple[MemeFormatCandidate, ...]
    recent_formats: tuple[str, ...] = ()  # 최근에 쓴 형식 key(반복 피하기)
    recent_jokes: tuple[str, ...] = ()
    hook: str = ""
    hook_sources: tuple[SourceItem, ...] = ()
    caution: str = ""
    force_key: str = ""  # "다른 밈으로 다시 만들기": 이 형식으로
    recent_mechanisms: tuple[str, ...] = ()  # 최근 밈의 웃음 작동 방식(같은 방식 반복 피하기, S18)


@dataclass(frozen=True)
class MemeOverlaySpec:
    kind: Literal["caption", "headline", "label", "stamp", "bubble"]
    text: str
    x: float
    y: float
    w: float
    align: Literal["left", "center", "right"] = "center"
    size: Literal["s", "m", "l", "xl"] = "m"
    tail_x: float | None = None
    tail_y: float | None = None


@dataclass(frozen=True)
class MemeRank:
    key: str
    score: float  # 0~10
    reason: str  # 확산 근거·주제 적합성·시각적 재미·최근 사용을 함께 본 한 줄


@dataclass(frozen=True)
class MemePage:
    """밈 게시물의 한 장(S18). 만화 컷 수(그림 안의 칸 나눔)와 게시물 장수는 다르다."""

    scene: str
    overlays: tuple[MemeOverlaySpec, ...] = ()


@dataclass(frozen=True)
class MemeFormatPlan:
    key: str  # 고른 형식
    why: str  # 고른 이유(사람이 읽음)
    funny_point: str  # 원 밈의 웃긴 포인트
    structure: str  # 원 밈의 구도·표정·동작·텍스트 배치 핵심
    reinterpretation: str  # 치이카와(주인공)로 바꾼 장면 한 줄
    scene: str  # 그림 생성 지시(카메라 거리·배경·동작·시선·표정·컷 나눔, 글자 자리 비우기)
    overlays: tuple[MemeOverlaySpec, ...]
    caption: str
    threads_text: str
    ranking: tuple[MemeRank, ...] = ()
    claims: tuple[tuple[str, str], ...] = ()
    suggested_character: str = ""
    # S18: 소재 출처·웃음이 작동하는 방식(한 줄)·고른 표현 형식과 그 이유, 게시물 장들(1~3)
    source_kind: str = ""
    mechanism: str = ""
    form: str = ""
    pages: tuple[MemePage, ...] = ()


@runtime_checkable
class ScenarioWriter(Protocol):
    def propose_meme_ideas(self, req: MemeRequest) -> list[MemeIdea]: ...
    def write_meme_script(self, req: MemeRequest, idea: MemeIdea) -> MemeScript: ...


@runtime_checkable
class MemeFormatPlanner(Protocol):
    """S17 제안: 밈 형식 재해석. ScenarioWriter와 따로 둬, 이 메서드가 없는 작가도 ScenarioWriter로 남는다(S17 리뷰)."""

    def plan_meme_format(self, req: MemeFormatRequest) -> MemeFormatPlan: ...


@runtime_checkable
class PhotoPlacer(Protocol):
    """실제 사진과 카드 장 짝짓기(S18 제안, ADR 0062). 그림을 볼 수 있는 공급자만 구현한다.

    cover_text: 표지 글, slides: (장 번호, 장 요약) 목록, photos: (로컬 경로, 출처 URL, 설명) 목록.
    돌려주는 값: {"cover": 사진 번호 또는 -1, "slides": [{"slide": 장 번호, "photo": 번호 또는 -1}],
    "photos": [{"photo": 번호, "shows": 무엇이 보이는지}]}. 검증은 호출자가 한다."""

    def place_photos(
        self, cover_text: str, slides: list[tuple[int, str]], photos: list[tuple[str, str, str]]
    ) -> dict: ...


@runtime_checkable
class CardRenderer(Protocol):
    def render(self, deck: CardDeck, out_dir: Path) -> list[Path]: ...


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, body: str, level: Literal["info", "warn", "error"]) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class CostSink(Protocol):
    """어댑터가 API 비용을 보고하는 곳. 저장소가 구현한다."""

    def record_cost(self, kind: CostKind, provider: str, usd: float, ref: str = "") -> None: ...


CostStatus = Literal["confirmed", "estimated", "unknown", "reserved", "released"]


@runtime_checkable
class CostLedger(CostSink, Protocol):
    """S13 제안: 전역 비용 원장. 유료 호출 전에 예약하고(reserve), 응답의 usage로 같은 행을 확정(confirm)하거나
    전송 전 실패면 해제(release), 전송 뒤 응답을 못 받았으면 미확인(mark_unknown)으로 남긴다.
    비용 상한은 confirmed+estimated+reserved(미확인 포함)를 더해 센다 — 미확인은 금액이 없으니 예약액으로."""

    def reserve(
        self, kind: CostKind, provider: str, estimate_usd: float, ref: str = "", model: str = ""
    ) -> str: ...
    def confirm(
        self,
        reservation_id: str,
        usd: float,
        status: Literal["confirmed", "estimated"] = "confirmed",
        detail: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> None: ...
    def release(self, reservation_id: str) -> None: ...
    def mark_unknown(self, reservation_id: str, request_id: str = "") -> None: ...
    def spent_between(self, start: datetime, end: datetime) -> float: ...
