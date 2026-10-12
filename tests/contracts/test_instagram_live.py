"""인스타그램 실계정 계약 테스트 (`uv run pytest -m contract tests/contracts/test_instagram_live.py`).

- 기본 실행에서는 빠진다(contract 마커). Aside CLI와 Aside 브라우저의 인스타그램 로그인이 필요하다.
- 게시 계정은 docs/day1-verification.md 표의 테스트 계정만. 만든 글은 끝나면 지운다.
- 답글은 다른 계정의 댓글이 필요해 NSM_CONTRACT_INSTAGRAM_COMMENT="작성자:댓글ID@글코드"가 있을 때만 돈다.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import pytest

from nsm.adapters.drivers import find_aside_cli
from nsm.adapters.platforms.instagram import InstagramAdapter
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
    m = re.search(r"^\| 인스타그램 \| (@[\w.]+)", text, re.M)
    assert m, "day1-verification.md에 인스타그램 테스트 계정이 없음"
    return m.group(1)


@pytest.fixture(scope="module")
def adapter(tmp_path_factory) -> InstagramAdapter:
    media = tmp_path_factory.mktemp("media")
    return InstagramAdapter(_account_from_day1_verification(), SystemClock(), media_dir=media)


def test_health_logged_in_as_test_account(adapter: InstagramAdapter) -> None:
    h = adapter.health()
    assert h.logged_in, h.detail


def test_publish_4x5_metrics_comments_then_delete(adapter: InstagramAdapter, caplog) -> None:
    card = adapter.media_dir / "cards" / "contract" / "slide-01.png"
    card.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "docs" / "samples" / "cards" / "info" / "slide-01.png", card)
    text = f"{VIRTUAL_PERSON_TEXT}\n\n[계약 테스트] 자동 점검 게시물입니다 {int(time.time())}\n곧 지웁니다."
    res = adapter.publish(
        RenderedPost(content_id="contract", platform="instagram", text=text, images=[card])
    )
    try:
        assert res.external_id, "게시 후 글 주소를 찾지 못함"
        assert "4:5가 아님" not in caplog.text  # 가상인물 배지가 잘리지 않음
        m = adapter.read_post_metrics(res.external_id)
        assert m.comments is not None
        assert isinstance(adapter.fetch_new_comments([res.external_id], None), list)
        assert adapter.read_account_metrics().followers is not None
    finally:
        if res.external_id:
            adapter.delete_post(res.external_id)


@pytest.mark.skipif(
    not os.environ.get("NSM_CONTRACT_INSTAGRAM_COMMENT"), reason="다른 계정의 댓글이 필요"
)
def test_reply_on_real_comment(adapter: InstagramAdapter) -> None:
    target, _, parent = os.environ["NSM_CONTRACT_INSTAGRAM_COMMENT"].partition("@")
    c = next(c for c in adapter.fetch_new_comments([parent], None) if c.external_id == target)
    adapter.reply(c, "[계약 테스트] 답글입니다")
