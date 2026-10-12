"""스레드 실계정 계약 테스트 (`uv run pytest -m contract tests/contracts/test_threads_live.py`).

- 기본 실행에서는 빠진다(contract 마커). Aside CLI와 Aside 브라우저의 스레드 로그인이 필요하다.
- 게시 계정은 docs/day1-verification.md 표의 테스트 계정만 쓴다(CLAUDE.md 가드레일). 만든 글은 끝나면 지운다.
- 댓글 답글·숨기기는 다른 계정의 댓글이 있어야 해서 NSM_CONTRACT_THREADS_COMMENT="작성자:코드@원글코드"가
  있을 때만 돈다.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import pytest

from nsm.adapters.drivers import find_aside_cli
from nsm.adapters.platforms.threads import ThreadsAdapter
from nsm.domain.clock import SystemClock
from nsm.domain.labels import VIRTUAL_PERSON_TEXT
from nsm.domain.models import RenderedPost

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(find_aside_cli() is None, reason="Aside CLI 없음"),
]

ROOT = Path(__file__).resolve().parents[2]


def _account_from_day1_verification() -> str:
    text = (ROOT / "docs" / "day1-verification.md").read_text(encoding="utf-8")
    m = re.search(r"^\| 스레드 \| (@[\w.]+)", text, re.M)
    assert m, "day1-verification.md에 스레드 테스트 계정이 없음"
    return m.group(1)


@pytest.fixture(scope="module")
def adapter(tmp_path_factory) -> ThreadsAdapter:
    media = tmp_path_factory.mktemp("media")
    return ThreadsAdapter(_account_from_day1_verification(), SystemClock(), media_dir=media)


def test_account_is_from_day1_verification() -> None:
    assert _account_from_day1_verification() == "@nonsleepmarketer"


def test_health_logged_in_as_test_account(adapter: ThreadsAdapter) -> None:
    h = adapter.health()
    assert h.logged_in, h.detail


def test_publish_metrics_comments_then_delete(adapter: ThreadsAdapter) -> None:
    card = adapter.media_dir / "cards" / "contract" / "slide-01.png"
    card.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "docs" / "samples" / "cards" / "info" / "slide-01.png", card)
    text = f"{VIRTUAL_PERSON_TEXT}\n\n[계약 테스트] 자동 점검 글입니다 {int(time.time())}\n곧 지웁니다."
    res = adapter.publish(
        RenderedPost(content_id="contract", platform="threads", text=text, images=[card])
    )
    try:
        assert res.url.endswith(f"/post/{res.external_id}")
        m = adapter.read_post_metrics(res.external_id)
        # 막 올린 글은 인사이트가 "—"(아직 없음)일 수 있어 조회수는 None을 허용한다
        assert m.likes is not None and m.comments is not None and m.shares is not None
        assert isinstance(adapter.fetch_new_comments([res.external_id], None), list)
        assert adapter.read_account_metrics().followers is not None
    finally:
        adapter.delete_post(res.external_id)


@pytest.mark.skipif(
    not os.environ.get("NSM_CONTRACT_THREADS_COMMENT"), reason="다른 계정의 댓글이 필요"
)
def test_reply_and_hide_on_real_comment(adapter: ThreadsAdapter) -> None:
    target, _, parent = os.environ["NSM_CONTRACT_THREADS_COMMENT"].partition("@")
    comments = adapter.fetch_new_comments([parent], None)
    c = next(c for c in comments if c.external_id == target)
    reply_code = adapter.reply(c, "[계약 테스트] 답글입니다")
    try:
        adapter.hide_comment(c)
        assert target not in [x.external_id for x in adapter.fetch_new_comments([parent], None)]
    finally:
        adapter.delete_post(reply_code)
