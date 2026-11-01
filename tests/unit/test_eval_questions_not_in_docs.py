"""R3d: 측정 질문은 문서에 원문으로 적지 않는다(process.md 4절). 새 문서에 들어가면 이 테스트가 실패한다.

이미 원문이 들어 있는 문서(규칙 전 기록과 사용자 안내 예시)는 아래 목록으로 허용한다. 그 조각은 측정에서
질문별로 빠진다(ADR 0057 개정 1). 목록에 새 파일을 더하지 말고, 질문을 "질문 1번"처럼 가리킨다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from nsm.rag.evaluate import MIN_NEEDLE, normalize

ROOT = Path(__file__).resolve().parents[2]
ALLOWED = {
    # R3d 전에 원문으로 적은 기록(고치지 않기로 함)
    "docs/devlog/2026-10-30-batch-R2.md",
    "docs/devlog/2026-10-30-batch-R3.md",
    "docs/devlog/2026-10-31-batch-R3b.md",
    "docs/devlog/2026-11-01-batch-R3c.md",
    "docs/adr/0057-rag-search-and-index-job.md",
    "docs/adr/0058-rag-doc-chat.md",
    # 사용자 안내의 질문 예시(일부러 보여 주는 것)
    "docs/user-guide.md",
}


def test_measurement_questions_are_not_quoted_in_new_docs() -> None:
    qs = yaml.safe_load((ROOT / "tests/fixtures/rag/questions.yaml").read_text("utf-8"))[
        "questions"
    ]
    needles = {q["q"]: normalize(q["q"]) for q in qs if len(normalize(q["q"])) >= MIN_NEEDLE}
    bad = []
    for f in sorted((ROOT / "docs").rglob("*.md")):
        rel = f.relative_to(ROOT).as_posix()
        if rel in ALLOWED or "/presentation/" in rel or "/learning/" in rel:
            continue
        text = normalize(f.read_text(encoding="utf-8"))
        bad += [f"{rel}: {q}" for q, n in needles.items() if n in text]
    assert not bad, "측정 질문이 문서에 원문으로 들어갔다(번호로 가리키세요): " + "; ".join(bad)
