"""대시보드 데모: 실제 데이터와 비슷한 예시 DB로 대시보드만 띄운다(배치 I 화면 점검용).

- 스케줄러·잡·외부 API를 부르지 않는다. 사용자의 ~/.nsm은 건드리지 않고 임시 폴더를 쓴다.
- 예시 데이터는 tests/fixtures(페르소나·자료·댓글·카드 덱)에서 만든다. 저장소 루트에서 실행한다.

    uv run python scripts/dashboard_demo.py [--port 8799] [--home 임시폴더] [--fresh]

`--fresh`는 "처음 설치한 사람" 화면(데이터 없음)을 띄운다.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
import yaml  # noqa: E402

from nsm.app.server import AppState, create_app  # noqa: E402
from nsm.config.schema import AppConfig  # noqa: E402
from nsm.domain.clock import SystemClock  # noqa: E402
from nsm.domain.models import AccountMetrics, Content, ContentBrief, PostMetrics  # noqa: E402
from nsm.engine.context import JobResult  # noqa: E402
from nsm.engine.publish import labeled_deck  # noqa: E402
from nsm.store.db import Database  # noqa: E402
from nsm.store.repo import Repository  # noqa: E402
from tests.fakes import FakeTextGenerator, load_comment_cases, load_source_items  # noqa: E402

_DEMO_POOL = ThreadPoolExecutor(max_workers=1)


def _demo_job(name: str) -> Future:
    """지금 실행 흉내: 3초 뒤 그럴듯한 결과(진행 표시·완료 표시 확인용, API 호출 없음)."""

    def work() -> JobResult:
        time.sleep(3)
        return JobResult(name, counts={"new": 12, "relevant": 4, "created": 1, "pending_review": 1})

    return _DEMO_POOL.submit(work)


def _now(fn) -> Future:
    f: Future = Future()
    try:
        f.set_result(fn())
    except Exception as e:  # noqa: BLE001
        f.set_exception(e)
    return f


def _config() -> AppConfig:
    raw = yaml.safe_load((ROOT / "tests/fixtures/personas/sample.yaml").read_text("utf-8"))
    return AppConfig.model_validate(raw)


def seed(repo: Repository, cfg: AppConfig, media: Path, reports: Path, renderer) -> None:
    clock = repo.clock
    now = clock.now()
    items = load_source_items()
    for i in items:
        repo.insert_source_item(i)
        repo.set_source_relevance(i.id, 0.9 if "코스피" not in i.title else 0.1)
    gen = FakeTextGenerator()

    def content(cid, template, status, reason=None, topic="핸드드립 물 온도"):
        g = gen.generate_content(
            ContentBrief(topic=topic, template=template, sources=items[:2]), cfg.persona, cfg.brand
        )
        g = g.model_copy(
            update={
                "instagram_caption": f"{topic}, 알고 보면 1도 차이가 맛을 바꿔요.\n\n저장해 두고 내릴 때마다 확인해 보세요 ☕\n\n#핸드드립 #홈카페",
                "threads_text": f"{topic}, 몇 도로 내리세요? 알고 보면 90~96도 사이에서 맛이 꽤 달라져요.",
            }
        )
        repo.insert_content(
            Content(
                id=cid,
                status=status,
                generated=g,
                relevance_confidence=0.93,
                created_at=now - timedelta(hours=2),
                review_reason=reason,
            )
        )
        return g

    content(
        "demo-review-1",
        "info",
        "pending_review",
        "보호 모드: 처음 20건은 사람이 확인",
        "핸드드립 물 온도",
    )
    content(
        "demo-review-2",
        "list",
        "pending_review",
        "페르소나 규칙 위반: 인스타그램 캡션 이모지 3개(상한 1개)",
        "원두 보관법 5가지",
    )
    if renderer is not None:
        for cid in ("demo-review-1",):
            renderer.render(labeled_deck(repo.get_content(cid)), media / "cards" / cid)
    content("demo-approved", "compare", "approved", topic="드립백 vs 핸드드립")
    repo.create_publications("demo-approved", ["threads", "instagram"])
    content("demo-published", "info", "approved", topic="그라인더 고르는 기준")
    for pid in repo.create_publications("demo-published", ["threads", "instagram"]):
        pub = repo.get_publication(pid)
        repo.transition_publication(pid, "publishing", increment_attempts=True)
        ext = f"P{pub.platform[:2].upper()}1"
        repo.transition_publication(
            pid, "published", external_id=ext, url=f"https://example.com/{pub.platform}/{ext}"
        )
        repo.insert_post_metrics(
            PostMetrics(
                platform=pub.platform,
                post_external_id=ext,
                captured_at=now,
                views=320 if pub.platform == "threads" else 540,
                reach=210,
                likes=18,
                comments=4,
                saves=7 if pub.platform == "instagram" else None,
                shares=2,
            )
        )
    repo.mark_content_published_if_complete("demo-published")
    # 실패한 게시 하나
    fails = repo.create_publications("demo-review-2", ["threads"])
    repo.transition_publication(fails[0], "publishing", increment_attempts=True)
    repo.transition_publication(
        fails[0],
        "failed",
        error="threads: 로그인 필요(Aside 브라우저)",
        next_attempt_at=now + timedelta(minutes=15),
    )
    for p in ("threads", "instagram"):
        repo.insert_account_metrics(AccountMetrics(platform=p, captured_at=now, followers=128))

    for c, cat in load_comment_cases():
        c = c.model_copy(update={"created_at": now - timedelta(minutes=30)})
        if not repo.insert_comment(c):
            continue
        key = repo.comment_key(c)
        if cat in ("complaint", "price_refund", "legal"):
            repo.update_comment(
                key,
                status="escalated",
                category=cat,
                reason={
                    "complaint": "불만",
                    "price_refund": "가격·환불 문의",
                    "legal": "법적 문제",
                }[cat],
            )
        elif cat == "spam":
            repo.update_comment(
                key,
                status="escalated",
                category=cat,
                reason="스팸 추정(이 플랫폼은 자동 숨김을 지원하지 않음)",
            )
        else:
            repo.update_comment(
                key,
                status="auto_replied",
                category=cat,
                action="auto_reply",
                reply_text="고마워요! 또 놀러 와 주세요",
            )

    for kind, usd in (("llm", 0.065), ("classifier", 0.0012), ("llm", 0.058)):
        repo.record_cost(kind, "anthropic", usd, "demo")
    wait = "인스타그램 요청 제한으로 쉬는 중(02:56 이후 다시)"
    for job, ok, msg in (
        (
            "collect",
            True,
            JobResult(
                "collect",
                counts={"new": 13, "duplicate": 4, "classified": 13, "relevant": 2, "bodies": 1},
            ).summary(),
        ),
        (
            "generate",
            True,
            JobResult(
                "generate",
                counts={"templates": ["info", "list"], "created": 2, "pending_review": 2},
            ).summary(),
        ),
        ("publish", False, "threads: 로그인 필요(Aside 브라우저)"),
        (
            "reply",
            True,
            JobResult("reply", counts={"new": 6, "auto_replied": 2, "escalated": 4}).summary(),
        ),
        # C10 전에 저장된 예전 형식 줄: 화면에서 문장으로 바뀌어 보여야 한다
        (
            "reply",
            True,
            f"reply: new=0, auto_replied=0, hidden=0, escalated=0, ignored=0, rate_limited=['{wait}']",
        ),
        ("health", True, "health: paused=[]"),
    ):
        rid = repo.start_run(job)
        repo.finish_run(rid, ok, msg)
    for title, body, level, link in (
        ("검토 대기 콘텐츠", "새 콘텐츠 2건이 검토를 기다립니다", "info", "/review"),
        ("댓글 확인 필요", "사람이 확인해야 할 댓글이 4건 있습니다", "warn", "/comments"),
        ("로그인 필요", "threads: Aside 브라우저에서 다시 로그인하세요", "error", "/"),
    ):
        repo.add_notification(title, body, level, link)
    reports.mkdir(parents=True, exist_ok=True)
    day = now.strftime("%Y-%m-%d")
    (reports / f"weekly-{day}.md").write_text(
        f"# 주간 리포트 ({day})\n\n## 이번 주 성과\n- 게시 4건, 평균 참여율 6.2%\n- 가장 반응 좋은 글: 그라인더 고르는 기준 (저장 7)\n\n"
        "## 다음 주 추천 주제\n- 원두 보관법\n- 핸드드립 물 온도\n\n## 비용\n- $0.42 (상한 $3.00/일)\n",
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("--fresh", action="store_true", help="데이터 없는 첫 설치 화면")
    ap.add_argument("--no-render", action="store_true", help="카드 PNG를 그리지 않음")
    args = ap.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix="nsm-demo-"))
    home.mkdir(parents=True, exist_ok=True)
    cfg = _config()
    if args.fresh:
        cfg = cfg.model_copy(update={"persona": cfg.persona.model_copy(update={"enabled": True})})
    db = Database(home / "data.duckdb")
    repo = Repository(db, SystemClock())
    renderer = None
    if not args.no_render:
        from nsm.render import get_renderer

        renderer = get_renderer()
    if not args.fresh and not repo.list_contents():
        seed(repo, cfg, home / "media", home / "reports", renderer)
    holder = {"cfg": cfg}
    state = AppState(
        repo=repo,
        clock=SystemClock(),
        get_config=lambda: holder["cfg"],
        set_config=lambda c: holder.__setitem__("cfg", c),
        submit=_now,
        adapters={},
        media_dir=home / "media",
        uploads_dir=home / "uploads",
        reports_dir=home / "reports",
        run_job_now=_demo_job,
        job_names=frozenset(
            {"collect", "generate", "publish", "reply", "metrics", "health", "weekly_report"}
        ),
        renderer=renderer,
    )
    print(f"데모 대시보드: http://127.0.0.1:{args.port}  (데이터: {home})")
    uvicorn.run(create_app(state), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
