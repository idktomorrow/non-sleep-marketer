import functools


@functools.cache
def app_version() -> str:
    """지금 켜진 프로그램의 버전(C26). 설치 정보에서 읽으므로 코드를 고쳐도 다시 켜기 전까지는 예전 값이다.

    켜져 있는 동안 바뀌지 않으므로 한 번만 읽는다(페이지마다 설치 정보를 뒤지지 않게).

    PR마다 pyproject.toml의 끝자리를 올린다(CLAUDE.md DoD 10). 화면에는 "v0.4.3"처럼 보인다.
    `uv run nsm run`은 켤 때 동기화하므로 값이 바뀐다. `--no-sync`로 켜면 예전 값이 남는다.
    """
    try:
        from importlib.metadata import version

        return version("non-sleep-marketer")
    except Exception:  # noqa: BLE001  설치 정보가 없을 때(패키지 없이 실행)만 pyproject를 읽는다
        import re
        from pathlib import Path

        try:
            text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text("utf-8")
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
            return m.group(1) if m else "dev"
        except OSError:
            return "dev"
