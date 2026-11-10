"""문서 질문 출처 측정(R4·R4b, ADR 0065): `questions_sources.yaml` 질문을 DB 복사본으로 돌려 출처별 표를 만든다.

- 운영 DB가 아니라 백업 복사본에서만 돌린다(`--db`). nsm run이 운영 DB를 쥐고 있고, 측정이 복사본의 장부·비용을 쓴다.
- 질문마다 출처 전체 / 문서 / 수집 자료 / 우리 글 네 번 `nsm.rag.search.search`를 부른다. 새 검색 로직은 없다.
  질문 임베딩은 질문당 한 번만 보낸다(같은 질문의 네 검색이 같은 벡터를 쓴다). Gemini는 질문 수만큼(5건)이다.
- 하루 장부(`nsm/rag/budget.py`)는 복사본 DB 기준으로 그대로 따른다. 막힌 시도도 1건으로 센다.
- 질문 문장이 그대로 든 조각은 그 질문에서만 빼고 센다(`leaked_by_question`, R3d·ADR 0057 개정 1).
- 출력은 devlog에 붙일 마크다운 표다. 질문 문장은 내보내지 않고 "질문 N번"으로만 쓴다.

    uv run python scripts/rag_measure_sources.py --db <복사본.duckdb> [--questions …] [--k 5] [--detail]

표를 만드는 `build_rows`·`render_table`은 네트워크 없는 순수 함수다(tests/unit/test_rag_measure_sources.py).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nsm.rag.search import Hit  # noqa: E402
from nsm.rag.sources_db import CARDS  # noqa: E402

KINDS = ("docs", "collected", "posts")  # 출처별 1등 점수 칸 순서(문서/수집/우리 글)
BADGE = {"docs": "문서", "collected": "수집", "posts": "우리 글"}
TARGET = {"collected": "수집 자료", "posts": "우리 글", "any": "둘 다"}
SCOPES = ("all", *KINDS)  # 질문마다 부르는 검색: 출처 전체, 그리고 출처 하나씩


def default_questions_path() -> Path:
    return ROOT / "tests" / "fixtures" / "rag" / "questions_sources.yaml"


@dataclass
class Row:
    n: int
    kind: str  # 질문 파일의 kind(collected·posts·any)
    top: tuple[str, float] | None  # 전체 검색 1등(출처, 점수)
    rank_all: int | None  # 전체 검색 상위 k 안의 정답 순위, 없으면 None
    rank_kind: dict[str, int | None]  # 그 출처만 검색한 정답 순위(any면 collected·posts 각각)
    counts: dict[str, int]  # 전체 상위 k의 출처별 개수
    top_by_kind: dict[str, float | None]  # 출처마다 1등 점수
    dup_cards: int  # 전체 상위 k 안에서 같은 원고가 인스타·스레드 두 경로로 함께 든 원고 수
    dup_slots: int = 0  # 그 원고들이 차지한 자리 수
    detail: list[Hit] = field(default_factory=list)  # 전체 상위 k(--detail 출력용)


def is_expected(path: str, expected: list[str]) -> bool:
    return any(path.startswith(e) for e in expected)


def rank_of(hits: list[Hit], expected: list[str]) -> int | None:
    for i, h in enumerate(hits, 1):
        if is_expected(h.path, expected):
            return i
    return None


def content_id(path: str) -> str | None:
    """우리 글 경로 `posts/<content_id>/<platform>`·`posts/<content_id>/cards`의 원고 id."""
    parts = path.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "posts" and parts[1] else None


def duplicate_cards(hits: list[Hit]) -> tuple[int, int]:
    """같은 원고가 두 플랫폼 경로로 함께 들어온 원고 수와 그 원고들이 차지한 자리 수.

    카드 문서 `posts/<content_id>/cards`(R4c)는 원고당 하나라 플랫폼으로 세지 않는다. 자리 수에는 그 원고의 카드
    조각도 넣는다(같은 원고가 차지한 자리).
    """
    platforms: dict[str, set[str]] = {}
    slots: dict[str, int] = {}
    for h in hits:
        cid = content_id(h.path)
        if cid is None:
            continue
        tail = h.path.split("/", 2)[2]
        if tail != CARDS:
            platforms.setdefault(cid, set()).add(tail)
        slots[cid] = slots.get(cid, 0) + 1
    dup = [cid for cid, ps in platforms.items() if len(ps) >= 2]
    return len(dup), sum(slots[cid] for cid in dup)


def build_rows(
    questions: list[dict[str, Any]], results: dict[int, dict[str, list[Hit]]], k: int = 5
) -> list[Row]:
    """질문 목록과 검색 결과(질문 번호 → 검색 범위(all·docs·collected·posts) → 점수 높은 순 Hit)로 표의 행을 만든다."""
    rows = []
    for q in questions:
        n = int(q["n"])
        kind = q["kind"]
        expected = list(q.get("expected", []))
        res = results.get(n, {})
        top_all = res.get("all", [])[:k]
        targets = ["collected", "posts"] if kind == "any" else [kind]
        dup, slots = duplicate_cards(top_all)
        rows.append(
            Row(
                n=n,
                kind=kind,
                top=(top_all[0].source_kind, top_all[0].score) if top_all else None,
                rank_all=rank_of(top_all, expected),
                rank_kind={t: rank_of(res.get(t, [])[:k], expected) for t in targets},
                counts={
                    kk: sum(1 for h in top_all if h.source_kind == kk)
                    for kk in KINDS
                    if any(h.source_kind == kk for h in top_all)
                },
                top_by_kind={kk: (res[kk][0].score if res.get(kk) else None) for kk in KINDS},
                dup_cards=dup,
                dup_slots=slots,
                detail=top_all,
            )
        )
    return rows


def _rank(r: int | None) -> str:
    return "-" if r is None else str(r)


def _score(s: float | None) -> str:
    return "-" if s is None else f"{s:.3f}"


def render_table(rows: list[Row], k: int = 5) -> str:
    head = (
        "| 질문 | 노린 출처 | 전체 검색 1등(점수) | 정답 순위(전체) | 정답 순위(그 출처만) | "
        f"전체 상위 {k}개의 출처 | 출처별 1등 점수(문서/수집/우리 글) | 중복 카드 |"
    )
    lines = [head, "|" + "---|" * 8]
    for r in rows:
        top = f"{BADGE.get(r.top[0], r.top[0])}({r.top[1]:.3f})" if r.top else "-"
        if len(r.rank_kind) == 1:
            rank_kind = _rank(next(iter(r.rank_kind.values())))
        else:
            rank_kind = " / ".join(f"{BADGE.get(t, t)} {_rank(v)}" for t, v in r.rank_kind.items())
        counts = ", ".join(
            f"{BADGE.get(kk, kk)} {c}" for kk, c in sorted(r.counts.items(), key=lambda x: -x[1])
        )
        scores = " / ".join(_score(r.top_by_kind.get(kk)) for kk in KINDS)
        dup = f"{r.dup_cards}({r.dup_slots}자리)" if r.dup_cards else "0"
        lines.append(
            f"| 질문 {r.n}번 | {TARGET.get(r.kind, r.kind)} | {top} | {_rank(r.rank_all)} | "
            f"{rank_kind} | {counts or '-'} | {scores} | {dup} |"
        )
    return "\n".join(lines)


def render_detail(rows: list[Row], questions: list[dict[str, Any]]) -> str:
    """해석용: 질문마다 전체 상위 k의 출처·점수·경로·제목 경로. 질문 문장은 넣지 않는다."""
    exp = {int(q["n"]): list(q.get("expected", [])) for q in questions}
    out = []
    for r in rows:
        out.append(f"질문 {r.n}번")
        for i, h in enumerate(r.detail, 1):
            mark = " ← 정답" if is_expected(h.path, exp[r.n]) else ""
            out.append(
                f"  {i}. [{BADGE.get(h.source_kind, h.source_kind)} {h.score:.3f}] {h.path} | {h.heading_path}{mark}"
            )
    return "\n".join(out)


class _OnceEmbedder:
    """질문 하나를 한 번만 보낸다. 같은 질문의 검색 네 번이 같은 벡터를 쓰고, 보낸 건수를 센다."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.model = inner.model
        self.dim = inner.dim
        self.sent = 0
        self._cache: dict[str, list[float]] = {}

    def embed_query(self, text: str) -> list[float]:
        if text not in self._cache:
            self.sent += 1  # 실패해도 보낸 것으로 센다(막힌 시도도 하루 한도에 세어짐)
            try:
                self._cache[text] = self.inner.embed_query(text)
            except Exception as e:
                if (
                    getattr(e, "code", "") == "not_sent"
                ):  # 분당 창이 차서 보내지도 않음(budget과 같은 판단)
                    self.sent -= 1
                raise
        return self._cache[text]


def measure(cfg: Any, repo: Any, embedder: Any, questions: list[dict[str, Any]], k: int, now):
    """복사본 DB에서 질문마다 네 범위를 검색한다. 반환: 질문 번호 → 범위 → Hit 목록, 뺀 조각 수."""
    from nsm.rag.budget import add_used, plan_embedding
    from nsm.rag.evaluate import leaked_by_question
    from nsm.rag.search import search

    plan = plan_embedding(cfg, repo, embedder, now, len(questions))
    if plan.embedder is None:
        raise SystemExit(f"측정할 수 없습니다: {plan.note}")
    if plan.max_embed < len(questions):
        raise SystemExit(
            f"복사본 장부로 오늘 남은 Gemini 한도가 {plan.max_embed}건이라 질문 {len(questions)}개를 다 잴 수 없습니다"
        )
    once = _OnceEmbedder(plan.embedder)
    texts = [q["q"] for q in questions]
    leaked: dict[str, set[str]] = {t: set() for t in texts}
    for kk in KINDS:
        for t, ids in leaked_by_question(repo.db, texts, once.model, once.dim, kk).items():
            leaked[t] |= ids
    results: dict[int, dict[str, list[Hit]]] = {}
    try:
        for q in questions:
            results[int(q["n"])] = {
                scope: search(
                    repo.db,
                    once,
                    q["q"],
                    k=k,
                    min_score=0.0,
                    source_kind=None if scope == "all" else scope,
                    exclude_ids=leaked[q["q"]],
                ).candidates
                for scope in SCOPES
            }
    finally:
        add_used(repo, now, once.sent)
    return results, len(set().union(*leaked.values()))


def main(argv: list[str] | None = None) -> int:
    import yaml

    from nsm.config.secrets import KeyringSecretStore
    from nsm.config.store import load_config
    from nsm.domain.clock import SystemClock
    from nsm.rag.budget import close_embedder
    from nsm.rag.command import open_db
    from nsm.rag.gemini import make_embedder
    from nsm.rag.indexer import stored_stats
    from nsm.store.repo import Repository

    p = argparse.ArgumentParser(
        description="문서 질문 출처 측정(R4b). 백업 복사본 DB에서만 돌린다."
    )
    p.add_argument("--db", type=Path, required=True, help="DuckDB 복사본 경로(운영 DB 아님)")
    p.add_argument("--questions", type=Path, default=default_questions_path())
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--detail", action="store_true", help="질문마다 상위 k 경로·제목 경로도 출력")
    a = p.parse_args(argv)
    if not a.db.is_file():
        print(f"DB 파일이 없습니다: {a.db}", file=sys.stderr)
        return 2
    questions = yaml.safe_load(a.questions.read_text(encoding="utf-8"))["questions"]
    cfg = load_config()
    db = open_db(a.db)
    try:
        repo = Repository(db, SystemClock())
        made = make_embedder(cfg, KeyringSecretStore(), repo)  # 비용은 복사본 costs 표에
        try:
            results, leaked = measure(cfg, repo, made, questions, a.k, datetime.now(UTC))
        finally:
            close_embedder(made)
        st = {kk: stored_stats(db, kk) for kk in KINDS}
        rows = build_rows(questions, results, a.k)
        print(
            "조각 "
            + " · ".join(
                f"{BADGE.get(kk, kk)} {s['chunks']:,}(벡터 없음 {s['without_vector']})"
                for kk, s in st.items()
            )
            + f" · 질문 문장이 든 조각 {leaked}개는 그 질문에서 뺌"
        )
        print()
        print(render_table(rows, a.k))
        if a.detail:
            print()
            print(render_detail(rows, questions))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
