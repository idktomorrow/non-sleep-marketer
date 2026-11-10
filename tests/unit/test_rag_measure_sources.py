"""R4b 출처 측정 스크립트의 표 만드는 순수 함수(네트워크·키 없음). 측정 자체는 devlog에 있다."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from nsm.rag.search import Hit

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rag_measure_sources.py"
_spec = importlib.util.spec_from_file_location("rag_measure_sources", _PATH)
m = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = m  # dataclass가 모듈을 찾을 수 있게
_spec.loader.exec_module(m)

QUESTION = "테스트용 질문 문장은 표에 나오면 안 된다"


def hit(path: str, score: float, kind: str) -> Hit:
    return Hit(path, f"{kind} > 제목", "본문", score, kind)


def q(n: int, kind: str, expected: list[str]) -> dict:
    return {"n": n, "q": QUESTION, "kind": kind, "expected": expected}


def test_rank_overall_and_in_target_source() -> None:
    qs = [q(1, "collected", ["source_items/A"])]
    res = {
        1: {
            "all": [
                hit("posts/C1/threads", 0.81, "posts"),
                hit("posts/C2/threads", 0.80, "posts"),
                hit("source_items/A", 0.79, "collected"),
            ],
            "docs": [hit("guide.md", 0.6, "docs")],
            "collected": [hit("source_items/A", 0.79, "collected")],
            "posts": [hit("posts/C1/threads", 0.81, "posts")],
        }
    }
    (row,) = m.build_rows(qs, res, k=5)
    assert row.top == ("posts", 0.81)
    assert row.rank_all == 3
    assert row.rank_kind == {"collected": 1}
    assert row.counts == {"posts": 2, "collected": 1}
    assert row.top_by_kind == {"docs": 0.6, "collected": 0.79, "posts": 0.81}


def test_rank_only_counts_within_k() -> None:
    qs = [q(1, "posts", ["posts/X/"])]
    all_hits = [hit(f"posts/C{i}/threads", 0.9 - i / 100, "posts") for i in range(5)]
    res = {1: {"all": [*all_hits, hit("posts/X/threads", 0.5, "posts")], "posts": all_hits}}
    (row,) = m.build_rows(qs, res, k=5)
    assert row.rank_all is None
    assert row.rank_kind == {"posts": None}


def test_kind_any_ranks_collected_and_posts_separately() -> None:
    qs = [q(2, "any", ["source_items/A", "posts/P/"])]
    res = {
        2: {
            "all": [hit("posts/P/instagram", 0.82, "posts")],
            "collected": [hit("source_items/B", 0.84, "collected")],
            "posts": [hit("posts/P/instagram", 0.82, "posts")],
        }
    }
    (row,) = m.build_rows(qs, res)
    assert row.rank_kind == {"collected": None, "posts": 1}
    table = m.render_table([row])
    assert "수집 - / 우리 글 1" in table
    assert "| 둘 다 |" in table


def test_duplicate_cards_counts_same_content_on_both_platforms() -> None:
    qs = [q(4, "posts", ["posts/A/"])]
    res = {
        4: {
            "all": [
                hit("posts/A/instagram", 0.82, "posts"),
                hit("posts/A/threads", 0.82, "posts"),
                hit("posts/A/threads", 0.81, "posts"),
                hit("posts/B/instagram", 0.80, "posts"),  # 한 플랫폼만: 중복 아님
                hit("posts/B/instagram", 0.79, "posts"),
            ]
        }
    }
    (row,) = m.build_rows(qs, res)
    assert (row.dup_cards, row.dup_slots) == (1, 3)
    assert "| 1(3자리) |" in m.render_table([row])


def test_duplicate_ignores_collected_and_docs() -> None:
    hits = [hit("source_items/A", 0.8, "collected"), hit("devlog/x.md", 0.7, "docs")]
    assert m.duplicate_cards(hits) == (0, 0)


def test_no_answer_and_no_results_show_dash() -> None:
    qs = [q(5, "posts", ["posts/Z/"])]
    (row,) = m.build_rows(qs, {}, k=5)
    assert row.top is None and row.rank_all is None
    line = m.render_table([row]).splitlines()[-1]
    assert line == "| 질문 5번 | 우리 글 | - | - | - | - | - / - / - | 0 |"


def test_question_text_never_in_output() -> None:
    qs = [q(1, "posts", ["posts/A/"])]
    res = {1: {"all": [hit("posts/A/threads", 0.8, "posts")], "posts": []}}
    rows = m.build_rows(qs, res)
    out = m.render_table(rows) + m.render_detail(rows, qs)
    assert QUESTION not in out
    assert "질문 1번" in out


class _Inner:
    model = "fake"
    dim = 2

    def __init__(self, fail_code: str | None = None) -> None:
        self.calls = 0
        self.fail_code = fail_code

    def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail_code is not None:
            e = RuntimeError("막힘")
            e.code = self.fail_code
            raise e
        return [1.0, 0.0]


def test_once_embedder_sends_each_question_once() -> None:
    inner = _Inner()
    once = m._OnceEmbedder(inner)
    for _ in range(4):
        once.embed_query("질문")
    once.embed_query("다른 질문")
    assert inner.calls == 2 and once.sent == 2


def test_once_embedder_counts_failed_send_but_not_not_sent() -> None:
    import pytest

    blocked = m._OnceEmbedder(_Inner("rate_limit"))
    with pytest.raises(RuntimeError):
        blocked.embed_query("질문")
    assert blocked.sent == 1  # 막힌 시도도 하루 한도에 세어진다
    unsent = m._OnceEmbedder(_Inner("not_sent"))
    with pytest.raises(RuntimeError):
        unsent.embed_query("질문")
    assert unsent.sent == 0


def test_cards_path_is_not_counted_as_a_platform() -> None:
    """R4c: 카드 문서(posts/<cid>/cards)는 원고당 하나라 한 플랫폼 글과 같이 와도 중복이 아니다."""
    one_platform = [hit("posts/A/cards", 0.82, "posts"), hit("posts/A/threads", 0.81, "posts")]
    assert m.duplicate_cards(one_platform) == (0, 0)
    both = [*one_platform, hit("posts/A/instagram", 0.80, "posts")]
    assert m.duplicate_cards(both) == (1, 3)  # 본문 두 벌은 여전히 중복, 자리 수엔 카드도 넣음
    assert m.content_id("posts/A/cards") == "A"
