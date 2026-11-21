"""C53 리뷰: 답글 생성 중 Claude 401/403도 키 오류 알림(하루 한 번). 처리 흐름은 예전 그대로."""

from __future__ import annotations

from nsm.engine import key_watch as kw
from nsm.engine.context import JobContext
from nsm.engine.reply import run_reply
from tests.engine.test_key_watch_c53 import _auth_error
from tests.fakes import FakeTextGenerator


def test_reply_401_alerts_key_once(ctx: JobContext) -> None:
    class Rejecting(FakeTextGenerator):
        def generate_reply(self, comment, persona, post_context):
            raise _auth_error()

    ctx.text_generator = Rejecting()
    r = run_reply(ctx)
    assert r.errors  # 예전처럼 오류로 남고 사람에게 넘긴다
    titles = [t for t, _, _ in ctx.notifier.calls]
    assert titles.count("Claude API 키 오류") == 1  # 댓글 여러 개여도 하루 한 번
    assert ctx.repo.get_setting(kw.CHECK_KEY)["anthropic_api_key"]["code"] == "invalid"


def test_reply_other_error_does_not_alert_key(ctx: JobContext) -> None:
    class Down(FakeTextGenerator):
        def generate_reply(self, comment, persona, post_context):
            raise RuntimeError("api down")

    ctx.text_generator = Down()
    run_reply(ctx)
    assert "Claude API 키 오류" not in [t for t, _, _ in ctx.notifier.calls]
