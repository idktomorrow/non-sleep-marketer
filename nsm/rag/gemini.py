"""Gemini 임베딩(R1, ADR 0056). 분류기(`nsm/adapters/classifier/gemini.py`)처럼 REST를 httpx로 직접 부른다.

- `batchEmbedContents`로 여러 조각을 한 요청에 묶고, 요청마다 `outputDimensionality`로 차원을 정한다.
- `gemini-embedding-2`는 `taskType`을 받지 않는다. 문서 조각은 공식 권장 형식 "title: … | text: …"로 보낸다
  (조각 첫 줄의 제목 경로를 title로).
- 묶음 요청 사이에 간격을 둔다. 429는 한 번 기다렸다 다시 보내고, 그래도 막히면 오류 종류 표의 `rate_limited`
  (`kind`·`retry_at`)로 올린다. 5xx·연결 오류는 `transient`.
- 비용은 `CostSink`에 `classifier` 종류로, ref "embedding"으로 적는다(CostKind는 동결, ADR 0056). 대시보드 비용 표에는
  "자료 분류·문서 색인"으로 보인다. 쓴 토큰이 없으면 적지 않는다.
- 키는 헤더로만 보내고 오류 문구에 남기지 않는다.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nsm.ports import AdapterError, CostSink

log = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
WINDOW_S = 60.0  # 분당 한도의 창
RATE_WAIT_S = 60.0  # 429 응답에 대기 시간(RetryInfo)이 없을 때 한 번 기다리는 초
RATE_WAIT_MAX_S = 120.0
# 무료 구간 한도(2026-10-30 실측, 429 응답의 quotaId): 모델별 분당 100건·하루 1,000건이고, 묶음 안의 조각 하나하나를
# 1건으로 센다. 막힌 시도도 하루 한도에 세어지는 것으로 보여(조각 230여 개만 저장됐는데 하루 한도 소진), 분당 한도에서만
# 한 번 기다렸다 다시 보내고 하루 한도면 바로 멈춘다
RATE_TRIES = 2
# 기다려도 막히면 이만큼 쉬었다 다시(색인은 다음 실행이 이어서 한다)
REST_AFTER_429 = timedelta(minutes=10)


def retry_delay(r: httpx.Response) -> float | None:
    """429 응답 본문의 RetryInfo.retryDelay("47s")를 초로. 없거나 못 읽으면 None."""
    try:
        for d in r.json().get("error", {}).get("details", []):
            if "RetryInfo" in str(d.get("@type", "")):
                return float(str(d.get("retryDelay", "")).rstrip("s"))
    except (ValueError, AttributeError, TypeError):
        return None
    return None


def quota_ids(r: httpx.Response) -> list[str]:
    """429 응답 본문 QuotaFailure의 한도 이름들(예: …PerDayPerUser…-FreeTier)."""
    try:
        return [
            str(v.get("quotaId", ""))
            for d in r.json().get("error", {}).get("details", [])
            if "QuotaFailure" in str(d.get("@type", ""))
            for v in d.get("violations", [])
        ]
    except (ValueError, AttributeError, TypeError):
        return []


class EmbeddingError(AdapterError):
    def __init__(
        self,
        message: str,
        kind: str = "transient",
        retry_at: datetime | None = None,
        code: str = "",
    ):
        super().__init__(message)
        self.kind = kind
        self.retry_at = retry_at
        self.code = code  # "interrupted"면 끄는 중(C33 판정)


def as_query(question: str) -> str:
    """질문 임베딩 형식(gemini-embedding-2 공식 권장, 검색 질문 쪽)."""
    return f"task: search result | query: {question.strip()}"


def as_document(text: str) -> str:
    """조각 첫 줄(제목 경로)을 title로, 나머지를 text로(gemini-embedding-2 권장 형식)."""
    title, _, body = text.partition("\n")
    return f"title: {title.strip() or 'none'} | text: {body.strip()}"


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 100,
        per_minute: int = 100,
        usd_per_mtok: float = 0.0,
        cost_sink: CostSink | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = api_key
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.per_minute = per_minute  # 분당 보낼 수 있는 건수(조각 하나 = 1건, 무료 구간 100)
        self.usd_per_mtok = usd_per_mtok
        self.cost_sink = cost_sink
        self.client = client or httpx.Client(timeout=60.0)
        self._sleep = sleep
        self._now = now
        self._mono = mono
        self.tokens = 0  # 이번 색인에서 쓴 입력 토큰(응답 usageMetadata)
        # 직전 1분 동안 보낸 (시각, 건수). 색인 묶음과 질문이 같은 창을 쓴다(R3b)
        self._sent: deque[tuple[float, int]] = deque()
        self.should_stop: Callable[[], bool] = lambda: False  # 끄는 중이면 기다림을 끊는다(R2 리뷰)

    def reset(self) -> None:
        """실행 한 번의 시작: 토큰을 이번 실행 것만 세게(임베더가 오래 살아서, R2 리뷰).
        보낸 기록(1분 창)은 지우지 않는다. 실제로 API에 보낸 건수라 실행이 바뀌어도 한도에 그대로 남는다."""
        self.tokens = 0

    def _make_room(self, n: int) -> None:
        """직전 1분 동안 보낸 건수 + 이번 n건이 분당 한도를 넘을 때만, 넘지 않을 만큼만 기다린다(R3b).

        예전에는 두 번째 요청부터 무조건 61초를 기다려, 색인 뒤에 던진 질문 하나가 63초 걸렸다
        (2026-10-31 21:28 운영 로그). 질문 1건은 색인이 막 100건을 보낸 직후가 아니면 기다리지 않는다.
        """
        need = min(n, self.per_minute)  # 한 번에 한도보다 많이 보내면 창이 빌 때까지만
        while True:
            t = self._mono()
            while self._sent and t - self._sent[0][0] >= WINDOW_S:
                self._sent.popleft()
            used = sum(c for _, c in self._sent)
            if used + need <= self.per_minute:
                return
            free, wait = 0, 0.0
            for at, c in self._sent:  # 오래된 것부터 빠져나가야 자리가 난다
                free += c
                wait = at + WINDOW_S - t
                if used - free + need <= self.per_minute:
                    break
            self._wait(max(wait, 0.0) + 0.5)  # 0.5초 여유(서버 시계와 어긋남)

    def _wait(self, seconds: float) -> None:
        """기다리되 1초마다 끄는 중인지 본다. 끄는 중이면 중단 오류(실행 기록엔 "중단")."""
        left = seconds
        while left > 0:
            if self.should_stop():
                raise EmbeddingError(
                    "프로그램 종료 중이라 임베딩을 멈춤", "transient", code="interrupted"
                )
            step = min(1.0, left)
            self._sleep(step)
            left -= step

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            part = texts[i : i + self.batch_size]
            out.extend(self._batch([as_document(t) for t in part], "embedding"))
        return out

    def embed_query(self, question: str) -> list[float]:
        """검색 질문 하나(R2). 비용 ref는 "embedding-query"로 문서 색인과 구분한다."""
        return self._batch([as_query(question)], "embedding-query")[0]

    def _batch(self, texts: list[str], ref: str) -> list[list[float]]:
        self._make_room(len(texts))  # 분당 한도를 넘을 때만 기다린다
        self._sent.append(
            (self._mono(), len(texts))
        )  # 막힌 시도도 한도에 세어지므로 보내기 전에 적는다
        body = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": self.dim,
                }
                for t in texts
            ]
        }
        data = self._post(body)
        try:
            vectors = [list(map(float, e["values"])) for e in data["embeddings"]]
        except (KeyError, TypeError, ValueError) as e:
            raise EmbeddingError(
                f"Gemini 임베딩 응답을 읽을 수 없음: {type(e).__name__}", "config_or_bug"
            ) from e
        if len(vectors) != len(texts) or any(len(v) != self.dim for v in vectors):
            raise EmbeddingError(
                f"Gemini 임베딩 응답 개수·차원이 다름({len(vectors)}개, 기대 {len(texts)}개·{self.dim}차원)",
                "config_or_bug",
            )
        tokens = int((data.get("usageMetadata") or {}).get("promptTokenCount") or 0)
        self.tokens += tokens
        if self.cost_sink is not None and tokens:
            # CostKind가 동결(llm·image·classifier)이라 새 종류를 못 넣는다: 생성이 아닌 판정·색인 쪽인 classifier로
            self.cost_sink.record_cost(
                "classifier", "gemini", tokens / 1_000_000 * self.usd_per_mtok, ref
            )
        return vectors

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{BASE_URL}/models/{self.model}:batchEmbedContents"
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        for attempt in range(RATE_TRIES):
            try:
                r = self.client.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                raise EmbeddingError(f"Gemini 임베딩 연결 오류({type(e).__name__})") from e
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                delay = retry_delay(r)
                if any("PerDay" in q for q in quota_ids(r)):
                    # 하루 한도: 다시 보내면 막힌 시도가 또 세어진다. 바로 멈추고, 풀리는 시각을 알린다
                    raise EmbeddingError(
                        "Gemini 임베딩 하루 한도(무료 구간 1,000조각)에 걸림. 내일 다시 실행하면 이어서 합니다",
                        "rate_limited",
                        self._now() + timedelta(seconds=delay or 86400),
                    )
                wait = min((delay or RATE_WAIT_S) + 1, RATE_WAIT_MAX_S)
                if attempt < RATE_TRIES - 1:
                    log.info("Gemini 임베딩 요청 제한(429): %.0f초 기다렸다 다시", wait)
                    self._wait(wait)
                    continue
                # 기다려도 막혔다: 오류 종류 표의 요청 제한으로 올린다
                raise EmbeddingError(
                    "Gemini 임베딩 요청 제한(HTTP 429, 무료 구간 분당 한도)",
                    "rate_limited",
                    self._now() + max(REST_AFTER_429, timedelta(seconds=wait)),
                )
            detail = " ".join(r.text[:200].split())
            kind = "transient" if r.status_code >= 500 else "config_or_bug"
            raise EmbeddingError(f"Gemini 임베딩 실패(HTTP {r.status_code}): {detail}", kind)
        raise EmbeddingError("Gemini 임베딩 실패")  # 도달하지 않음


def make_embedder(
    cfg: Any, secrets: Any, cost_sink: CostSink | None = None
) -> GeminiEmbedder | None:
    """Gemini 키가 있으면 임베더, 없으면 None(조각만 저장하고 벡터는 다음에)."""
    key = secrets.get("gemini_api_key")
    if not key:
        return None
    r = cfg.settings.rag
    return GeminiEmbedder(
        key,
        r.embedding_model,
        r.embedding_dim,
        batch_size=r.embed_batch_size,
        per_minute=r.embed_per_minute,
        usd_per_mtok=r.embedding_usd_per_mtok,
        cost_sink=cost_sink,
    )
