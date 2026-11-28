"""잡 이름만 모은 가벼운 모듈(C61). `nsm --help`가 잡 전부를 import하지 않고도(약 2초) 이름을 보이게.

원본은 `nsm.engine.runner.JOBS`·`ARG_JOBS`다. runner가 import될 때 이 목록과 키가 같은지 확인하고
다르면 바로 실패한다(새 잡을 더하고 여기를 잊으면 첫 테스트부터 깨진다).
"""

from __future__ import annotations

JOB_NAMES: tuple[str, ...] = (
    "collect",
    "generate",
    "publish",
    "reply",
    "health",
    "metrics",
    "weekly_report",
    "maintenance",
    "index",
)
ARG_JOB_NAMES: tuple[str, ...] = ("keyword",)


def job_help() -> str:
    """`nsm run --once --job` 도움말: collect|generate|...|keyword"""
    return "--once와 함께: " + "|".join((*JOB_NAMES, *ARG_JOB_NAMES))
