"""C47 문구: 문서 질문 힌트 예시, 오늘 화면 "쓸 수 있는 자료" 기준 한 마디."""

from __future__ import annotations

import re
from pathlib import Path

from nsm.rag.answer import HINT
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처

ASK = Path(__file__).parents[2] / "nsm" / "app" / "templates" / "ask.html"


def test_hint_example_is_one_of_the_example_chips() -> None:
    chips = re.search(r"ask_examples = \[(.*?)\]", ASK.read_text("utf-8")).group(1)
    example = re.search(r"\(예: (.+)\)$", HINT).group(1)
    assert f'"{example}"' in chips
    assert "보호 모드가 뭐야" not in HINT  # R4 측정 질문 원문은 쓰지 않는다


def test_usable_material_card_says_before_same_news_filter(env) -> None:  # noqa: F811
    _post(env, "/ui-mode", mode="advanced")
    html = env["client"].get("/").text
    i = html.index("쓸 수 있는 자료")
    card = html[i : html.index("</div>", i)]
    assert "같은 소식 거르기 전" in card
    assert 'class="muted small one-line"' in card and 'title="' in card  # 한 줄, 전체 뜻은 title
