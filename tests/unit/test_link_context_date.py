"""링크 요약문에 결과 항목의 날짜를 붙인다(S16): 네이버 새 결과 화면은 날짜가 링크 블록 두세 단계 위에 있다.

실제 Chromium으로 고정 HTML을 띄워 드라이버와 같은 스크립트를 돌린다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nsm.adapters.drivers.base import LINK_CONTEXT_JS
from nsm.adapters.sources.web import parse_naver_date

HTML = """
<ul class="list_news">
  <li><div class="item">
    <div class="press"><span>일간스포츠</span><span>2시간 전</span></div>
    <div class="body"><div class="title"><a id="a1" href="https://isplus.com/1">200만 눈앞 4DX 상영 확정</a></div>
      <div class="desc">21일 배급사에 따르면 24일, 25일 양일간 상영된다.</div></div>
  </div></li>
  <li><div class="item">
    <div class="press"><span>세계일보</span><span>1일 전</span></div>
    <div class="body"><div class="title"><a id="a2" href="https://segye.com/2">세이렌이 부르는 잔혹 동화</a></div></div>
  </div></li>
  <li><div class="item"><div class="body"><div class="title">
    <a id="a3" href="https://x.example/3">날짜 표기가 없는 결과</a></div></div></div></li>
</ul>
"""


def test_each_link_gets_its_own_item_date() -> None:
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(HTML)
            ctx = {
                i: page.evaluate(
                    f"(sel) => ({LINK_CONTEXT_JS.strip()})(document.querySelector(sel))", f"#{i}"
                )
                for i in ("a1", "a2", "a3")
            }
            browser.close()
    except Exception as e:  # noqa: BLE001  브라우저가 없는 환경
        pytest.skip(f"Chromium 없음: {e}")
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert parse_naver_date(ctx["a1"], now) == now - timedelta(hours=2)
    assert parse_naver_date(ctx["a2"], now) == now - timedelta(days=1)  # 옆 항목 날짜를 빌리지 않음
    assert parse_naver_date(ctx["a3"], now) is None  # 목록(UL)을 넘어 다른 항목까지 올라가지 않음
