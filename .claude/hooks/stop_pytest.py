"""Claude Code Stop 훅: batch/ 브랜치에서 테스트가 실패하면 세션 종료를 막는다.

- stop_hook_active가 true면 통과(훅 무한 루프 방지)
- 현재 브랜치가 batch/로 시작하지 않으면 통과
- pyproject.toml이 없으면 통과
- `uv run pytest -q` 실패 시 exit 2 + stderr로 실패 요약을 Claude에게 돌려준다
"""

import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if payload.get("stop_hook_active"):
        return 0
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").exists():
        return 0
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    if not branch.startswith("batch/"):
        return 0
    result = subprocess.run(
        ["uv", "run", "pytest", "-q", "-x"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        return 0
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-30:])
    print(f"batch 브랜치에서 테스트가 실패했습니다. DoD 미충족:\n{tail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
