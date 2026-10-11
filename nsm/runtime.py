"""`nsm run` 진입점. 배치 E(잡 1회 실행)와 배치 F(스케줄러 + 대시보드)에서 구현한다."""

from __future__ import annotations


def run_job_once(job: str) -> None:
    raise SystemExit(f"잡 실행은 배치 E에서 구현됩니다 (요청: {job})")


def run_forever() -> None:
    raise SystemExit("`nsm run`은 배치 F에서 구현됩니다")
