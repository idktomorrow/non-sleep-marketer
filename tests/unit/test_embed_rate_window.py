"""R3b: Gemini 임베딩 분당 한도를 "직전 1분에 보낸 건수"로 지킨다. 가짜 시계·가짜 sleep, 네트워크 없음.

2026-10-31 21:28 운영 로그: 두 번째 질문이 Gemini 요청 전에 61초를 기다려 63초 걸렸다(Claude는 1.7초).
"""

from __future__ import annotations

import httpx
import pytest

from nsm.rag.gemini import EmbeddingError, GeminiEmbedder


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*a, **k):
        raise AssertionError("테스트에서 네트워크 호출 금지")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)


class Clock:
    """sleep을 부르면 그만큼 시간이 간다."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def mono(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def _embedder(clock: Clock, sent: list[tuple[float, int]]) -> GeminiEmbedder:
    def handler(req: httpx.Request) -> httpx.Response:
        n = len(__import__("json").loads(req.content)["requests"])
        sent.append((clock.t, n))
        return httpx.Response(
            200,
            json={
                "embeddings": [{"values": [0.1] * 4} for _ in range(n)],
                "usageMetadata": {"promptTokenCount": n},
            },
        )

    return GeminiEmbedder(
        "k" * 30,
        "gemini-embedding-2",
        4,
        batch_size=100,
        per_minute=100,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        mono=clock.mono,
    )


def test_single_question_does_not_wait() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed_query("보호 모드가 뭐야")
    e.embed_query("429가 나면 어떻게 돼")  # 63초 사례: 두 번째 질문도 기다리지 않는다
    assert clock.slept == [] and len(sent) == 2


def test_batch_right_after_a_full_batch_waits_only_the_remaining_time() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed([f"t\n{i}" for i in range(100)])  # 색인 묶음 100건: 1분 창이 찼다
    clock.t += 20
    e.embed(["t\n다음 묶음"])
    assert 40 <= sum(clock.slept) <= 41  # 남은 40초(+여유 0.5초)만
    assert sent[-1][0] - sent[0][0] >= 60


def test_question_waits_a_little_but_not_a_minute() -> None:
    """질문은 10초 안이면 기다렸다 보내고, 더 길면 기다리지 않고 "약 N초 뒤"로 바로 알린다(화면을 붙잡지 않게)."""
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed([f"t\n{i}" for i in range(100)])
    clock.t += 20  # 40초 남음
    with pytest.raises(EmbeddingError) as exc:
        e.embed_query("보호 모드가 뭐야")
    assert exc.value.code == "not_sent" and exc.value.kind == "rate_limited"
    assert "약 41초 뒤" in str(exc.value) and clock.slept == [] and len(sent) == 1
    clock.t += 35  # 5초 남음
    e.embed_query("보호 모드가 뭐야")
    assert 5 <= sum(clock.slept) <= 6 and len(sent) == 2


def test_small_batch_after_partial_window_does_not_wait() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed([f"t\n{i}" for i in range(3)])  # 바뀐 조각 3개만 색인
    e.embed_query("질문")
    assert clock.slept == []


def test_consecutive_batches_never_exceed_100_per_minute() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed([f"t\n{i}" for i in range(450)])  # 100·100·100·100·50
    assert [n for _, n in sent] == [100, 100, 100, 100, 50]
    for i, (t, _) in enumerate(sent):
        in_window = sum(n for t2, n in sent[: i + 1] if t - t2 < 60)
        assert in_window <= 100
    assert 4 * 60 <= sent[-1][0] - sent[0][0] <= 4 * 61 + 5


def test_waiting_stops_when_shutting_down() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.embed([f"t\n{i}" for i in range(100)])
    e.should_stop = lambda: True
    with pytest.raises(EmbeddingError) as exc:
        e.embed(["t\n다음 묶음"])  # 창이 찼으니 기다려야 하는데, 끄는 중이라 바로 끊는다
    assert exc.value.code == "interrupted" and clock.slept == [] and len(sent) == 1


def test_old_interval_setting_is_accepted_but_ignored(app_config) -> None:
    from nsm.config.schema import RagSettings
    from nsm.config.secrets import MemorySecretStore
    from nsm.rag.gemini import make_embedder

    r = RagSettings(embed_interval_seconds=61)  # 예전 config.yaml에 있던 값
    app_config.settings.rag = r
    e = make_embedder(
        app_config, MemorySecretStore({"gemini_api_key": "fake-gemini-key-AAAAAAAAAAAAAAAAAAAA"})
    )
    assert e.per_minute == 100 and not hasattr(e, "interval_s")


def test_retry_after_429_is_counted_in_the_window() -> None:
    """429 뒤 다시 보낸 묶음도 1분 창에 센다(창이 실제보다 적게 세지 않게)."""
    clock = Clock()
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        n = len(__import__("json").loads(req.content)["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 4} for _ in range(n)]})

    e = GeminiEmbedder(
        "k" * 30,
        "m",
        4,
        per_minute=100,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        mono=clock.mono,
    )
    e.embed([f"t\n{i}" for i in range(50)])
    assert e._used == 100  # 처음 50 + 다시 보낸 50


def test_batches_larger_than_the_per_minute_limit_are_split() -> None:
    clock, sent = Clock(), []
    e = _embedder(clock, sent)
    e.per_minute = 30
    e.embed([f"t\n{i}" for i in range(70)])
    assert [n for _, n in sent] == [30, 30, 10]


def test_concurrent_callers_never_overflow_the_window() -> None:
    """색인(작업 스레드)과 질문(웹 스레드)이 동시에 보내도 1분에 한도를 넘지 않는다."""
    import threading
    import time as real_time

    sent: list[tuple[float, int]] = []
    lock = threading.Lock()

    def handler(req: httpx.Request) -> httpx.Response:
        n = len(__import__("json").loads(req.content)["requests"])
        with lock:
            sent.append((real_time.monotonic(), n))
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 4} for _ in range(n)]})

    e = GeminiEmbedder(
        "k" * 30,
        "m",
        4,
        per_minute=10,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,  # 기다리지 않음: 창이 차면 query는 바로 not_sent
    )
    errors: list[str] = []

    def ask() -> None:
        try:
            e.embed_query("q")
        except EmbeddingError as err:
            errors.append(err.code)

    ts = [threading.Thread(target=ask) for _ in range(30)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(n for _, n in sent) == 10 and errors.count("not_sent") == 20
