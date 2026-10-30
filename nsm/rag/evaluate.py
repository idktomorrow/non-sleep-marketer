"""검색 측정(R3, ADR 0058): 질문 파일 20개를 지금 DB로 돌려 R2 devlog와 같은 표를 만든다.

- 검색만 한다. Claude는 부르지 않는다(Gemini 질문 임베딩 20건만, 하루 장부를 탄다).
- 질문은 한 번씩만 임베딩하고 상위 후보·점수를 받아, 기준값별 표는 그 점수로 계산한다(R2와 같은 방법).
- 결과는 `doc_eval`에 시각과 함께 남긴다. 벡터가 다 찬 뒤 다시 눌러 전후를 비교한다.
- 화면에 둔 이유: nsm run이 운영 DB를 쥐고 있어 CLI로는 운영 DB를 잴 수 없다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from nsm.rag.budget import add_used, plan_embedding
from nsm.rag.indexer import stored_stats
from nsm.rag.search import search
from nsm.store.db import from_db, to_db

THRESHOLDS = (0.60, 0.65, 0.70)


def default_questions_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rag" / "questions.yaml"


class EvalRejected(Exception):
    pass


@dataclass
class EvalRow:
    threshold: float
    answered: int  # 답 있는 질문 중 기준값을 넘은 것
    top1: int
    top3: int
    rejected: int  # 답 없는 질문 중 "근거 없음"
    answerable: int
    unanswerable: int


@dataclass
class EvalResult:
    ran_at: datetime
    chunks: int
    without_vector: int
    rows: list[EvalRow] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def load_questions(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise EvalRejected(f"질문 파일이 없습니다: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]


def table(details: list[dict[str, Any]], thresholds=THRESHOLDS) -> list[EvalRow]:
    ans = [d for d in details if d["answerable"]]
    no = [d for d in details if not d["answerable"]]
    rows = []
    for th in thresholds:
        rows.append(
            EvalRow(
                threshold=th,
                answered=sum(1 for d in ans if d["cands"] and d["cands"][0]["score"] >= th),
                top1=sum(
                    1
                    for d in ans
                    if d["cands"]
                    and d["cands"][0]["score"] >= th
                    and d["cands"][0]["path"] in d["expected"]
                ),
                top3=sum(
                    1
                    for d in ans
                    if any(c["score"] >= th and c["path"] in d["expected"] for c in d["cands"][:3])
                ),
                rejected=sum(1 for d in no if not d["cands"] or d["cands"][0]["score"] < th),
                answerable=len(ans),
                unanswerable=len(no),
            )
        )
    return rows


def run_eval(
    cfg: Any, repo: Any, embedder: Any | None, now: datetime, questions_path: Path | None = None
) -> EvalResult:
    qs = load_questions(questions_path or default_questions_path())
    plan = plan_embedding(cfg, repo, embedder, now, len(qs))
    if plan.embedder is None:
        raise EvalRejected(f"측정할 수 없습니다: {plan.note}")
    if plan.max_embed < len(qs):
        raise EvalRejected(
            f"오늘 남은 Gemini 한도가 {plan.max_embed}건이라 질문 {len(qs)}개를 다 잴 수 없습니다. 내일(한국 오후 4~5시 뒤) 다시 눌러 주세요"
        )
    st = stored_stats(repo.db, "docs")
    out = EvalResult(ran_at=now, chunks=st["chunks"], without_vector=st["without_vector"])
    sent = 0
    for q in qs:
        try:
            res = search(repo.db, plan.embedder, q["q"], k=5, min_score=0.0)
        except Exception as e:  # noqa: BLE001  한도 등으로 중간에 멈추면 거기까지
            add_used(repo, now, sent + 1)  # 막힌 시도도 하루 한도에 세어진다
            raise EvalRejected(
                f"{len(out.details) + 1}번째 질문에서 멈춰 측정을 남기지 않았습니다"
                f"({getattr(e, 'kind', '') or type(e).__name__}). 질문 일부로 만든 표는 다른 측정과 비교되지 않습니다"
            ) from e
        sent += 1
        out.details.append(
            {
                "q": q["q"],
                "answerable": bool(q["answerable"]),
                "expected": q.get("expected", []),
                "cands": [
                    {"path": h.path, "heading": h.heading_path, "score": round(h.score, 4)}
                    for h in res.candidates
                ],
            }
        )
    add_used(repo, now, sent)
    out.rows = table(out.details)
    save_eval(repo.db, out)
    return out


def save_eval(db: Any, r: EvalResult) -> None:
    db.execute(
        "INSERT INTO doc_eval VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            uuid.uuid4().hex,
            to_db(r.ran_at),
            r.chunks,
            r.without_vector,
            json.dumps([row.__dict__ for row in r.rows]),
            json.dumps(r.details, ensure_ascii=False),
            r.error or None,
        ],
    )


def recent_evals(db: Any, limit: int = 5) -> list[EvalResult]:
    rows = db.execute(
        "SELECT ran_at, chunks, without_vector, summary, details, error FROM doc_eval ORDER BY ran_at DESC LIMIT ?",
        [limit],
    )
    out = []
    for ran, chunks, wv, summary, details, err in rows:
        out.append(
            EvalResult(
                ran_at=from_db(ran),
                chunks=chunks,
                without_vector=wv,
                rows=[EvalRow(**r) for r in json.loads(summary)],
                details=json.loads(details),
                error=err or "",
            )
        )
    return out
