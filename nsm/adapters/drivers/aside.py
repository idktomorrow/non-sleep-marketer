"""Aside 브라우저를 `aside repl`(Playwright 형식 JS)로 조작한다 (ADR 0017, 배치 G).

자연어 명령(`aside "..."`)은 쓰지 않는다. 게시 문구가 에이전트 지시로 읽히면 프롬프트 인젝션이 되고,
매번 동작이 달라진다. 대신 고정된 JS 템플릿을 실행하고 값은 데이터로만 넘긴다.

- 실행: `aside repl <js>`를 셸 없이 리스트 인자로 부른다. 한 번 부를 때마다 새 세션(120초 제한).
- 값 전달: 인자 dict를 JSON → base64 한 덩어리로 넣고, JS가 풀어서 `ARGS`로 쓴다. 문자열을 JS 소스에 이어 붙이지 않는다.
- 결과: JS가 `@@NSM@@{json}` 한 줄을 찍고, 여기서 그 줄만 읽는다.
- 파일: repl은 자기 세션 폴더 밖 파일을 못 읽는다(1일차 검증). 그래서 넘길 파일만 127.0.0.1 임시 HTTP 서버
  (무작위 포트·무작위 경로)로 내주고, JS가 받아 세션 폴더에 쓴 뒤 업로드한다. 서버는 호출이 끝나면 닫는다.
"""

from __future__ import annotations

import base64
import http.server
import json
import logging
import re
import secrets
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nsm.adapters.drivers import find_aside_cli
from nsm.adapters.drivers.base import DriverUnavailableError, Link

log = logging.getLogger(__name__)

MARKER = "@@NSM@@"
REPL_LIMIT_S = 120  # aside repl 자체 제한
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# 오류 메시지를 DB·대시보드에 남기기 전에 지운다(쿠키·토큰·세션 값처럼 보이는 것)
_SECRETISH = re.compile(
    r"(?i)(cookie|set-cookie|authorization|token|sessionid|csrftoken|ds_user_id)\S*\s*[:=]\s*\S+"
)


class AsideError(RuntimeError):
    """repl 실행 실패. `code`로 원인을 나눈다: login, mismatch, timeout, script, cli."""

    def __init__(self, message: str, code: str = "script") -> None:
        super().__init__(message)
        self.code = code


def scrub(text: str, limit: int = 300) -> str:
    text = _ANSI.sub("", text or "")
    text = _SECRETISH.sub(r"\1=<지움>", text)
    text = " ".join(text.split())
    return text[-limit:] if len(text) > limit else text


# JS 쪽 공통 도우미. 스크립트 본문은 async 함수 몸통이고 값을 return 하면 결과가 된다.
_PRELUDE = r"""
const ARGS = JSON.parse(Buffer.from(__PAYLOAD__, 'base64').toString('utf8'));
const __opened = [];
class NsmError extends Error { constructor(code, msg) { super(msg); this.code = code; } }
async function nsmOpen(url) { const p = await openTab(url); __opened.push(p); return p; }
async function nsmWait(loc, ms) { await loc.first().waitFor({ timeout: ms || 20000 }); return loc.first(); }
async function nsmFiles() {
  const out = [];
  for (const f of (ARGS.__files || [])) {
    const r = await fetch(f.url);
    if (!r.ok) throw new NsmError('script', 'file fetch ' + r.status);
    const local = './' + f.name;
    await fs.writeFile(local, Buffer.from(await r.arrayBuffer()));
    out.push(local);
  }
  return out;
}
"""

_WRAP = r"""
let __res;
try {
  __res = { ok: true, data: await (async () => { __BODY__ })() };
} catch (e) {
  __res = { ok: false, code: (e && e.code) || 'script', error: String((e && e.message) || e) };
} finally {
  for (const p of __opened) { try { await closeTab(p); } catch (e) {} }
}
console.log(__MARKER__ + JSON.stringify(__res));
"""


def build_script(body: str, args: dict[str, Any]) -> str:
    payload = base64.b64encode(json.dumps(args, ensure_ascii=False).encode("utf-8")).decode()
    # str.replace: 본문 JS에 % 같은 문자가 있어도 깨지지 않게(본문은 코드 상수, 값은 payload로만)
    prelude = _PRELUDE.replace("__PAYLOAD__", json.dumps(payload))
    wrap = _WRAP.replace("__MARKER__", json.dumps(MARKER))
    return prelude + wrap.replace("__BODY__", body)


def parse_output(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        line = _ANSI.sub("", line).strip()
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER) :])
    raise AsideError(f"Aside 결과 줄이 없음: {scrub(stdout)}", code="cli")


class _OneShotFiles:
    """지정한 파일만 무작위 경로로 내주는 127.0.0.1 HTTP 서버."""

    def __init__(self, files: list[Path]) -> None:
        self.token = secrets.token_urlsafe(16)
        self.routes: dict[str, Path] = {}
        self.names: list[str] = []
        for i, f in enumerate(files):
            name = f"f{i:02d}{f.suffix.lower()}"
            self.routes[f"/{self.token}/{name}"] = f
            self.names.append(name)
        routes = self.routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = routes.get(self.path)
                if path is None:
                    self.send_error(404)
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "image/png" if path.suffix == ".png" else "application/octet-stream",
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a: object) -> None:  # 접근 로그를 남기지 않는다
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def entries(self) -> list[dict[str, str]]:
        port = self.server.server_address[1]
        return [{"name": n, "url": f"http://127.0.0.1:{port}/{self.token}/{n}"} for n in self.names]


@contextmanager
def serve_files(files: list[Path]) -> Iterator[list[dict[str, str]]]:
    if not files:
        yield []
        return
    srv = _OneShotFiles(files)
    srv.thread.start()
    try:
        yield srv.entries()
    finally:
        srv.server.shutdown()
        srv.server.server_close()


class AsideRepl:
    """`aside repl` 한 번 = JS 한 번. 결과 dict를 돌려주거나 AsideError를 낸다."""

    def __init__(self, exe: Path | None = None, account: str | None = None) -> None:
        self.exe = exe or find_aside_cli()
        self.account = account

    def available(self) -> bool:
        return self.exe is not None

    def run(
        self, body: str, args: dict[str, Any] | None = None, files: list[Path] | None = None
    ) -> Any:
        if self.exe is None:
            raise DriverUnavailableError(
                "Aside CLI가 없습니다. https://docs.aside.com/help/developers 에서 설치하세요."
            )
        with serve_files(list(files or [])) as entries:
            script = build_script(body, {**(args or {}), "__files": entries})
            cmd = [str(self.exe), "repl"]
            if self.account:
                cmd += ["--account", self.account]
            cmd.append(script)
            try:
                proc = subprocess.run(  # noqa: S603  셸 없이 고정 실행 파일 + 리스트 인자
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=REPL_LIMIT_S + 30,
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                raise AsideError("Aside 응답 시간 초과", code="timeout") from e
        res = parse_output(proc.stdout)
        if not res.get("ok"):
            raise AsideError(scrub(res.get("error", "")), code=res.get("code") or "script")
        return res.get("data")


_LINKS_BODY = r"""
const pg = await nsmOpen(ARGS.url);
await sleep(1500);
const links = await pg.evaluate(() => Array.from(document.querySelectorAll('a[href]')).map(a => {
  const block = a.closest('li, article, section, div');
  const ctx = block ? block.innerText : a.innerText;
  return { text: (a.innerText || '').trim(), href: a.href, context: (ctx || '').trim().slice(0, 400) };
}));
const text = await pg.evaluate(() => document.body.innerText);
return { links, text };
"""


class AsideDriver:
    """수집용 BrowserDriver. goto 때 페이지를 한 번 읽어 링크와 본문을 함께 받아 둔다."""

    def __init__(self, repl: AsideRepl | None = None) -> None:
        self.repl = repl or AsideRepl()
        if not self.repl.available():
            raise DriverUnavailableError("Aside CLI가 없습니다")
        self._page: dict[str, Any] = {"links": [], "text": ""}

    def goto(self, url: str) -> None:
        self._page = self.repl.run(_LINKS_BODY, {"url": url}) or {"links": [], "text": ""}

    def links(self) -> list[Link]:
        return [Link(**d) for d in self._page.get("links", [])]

    def page_text(self) -> str:
        return str(self._page.get("text", ""))

    def close(self) -> None:
        self._page = {"links": [], "text": ""}
