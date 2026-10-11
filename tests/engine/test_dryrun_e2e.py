"""드라이런 전체 흐름: 수집 → 생성 → 게시가 미리보기 폴더에 PNG와 원고를 남긴다 (S4 마지막 시나리오)."""

from __future__ import annotations

from pathlib import Path

from nsm.adapters.platforms.dryrun import DryRunAdapter
from nsm.config.store import parse_windows
from nsm.domain.labels import BLOG_TITLE_SUFFIX, VIRTUAL_PERSON_TEXT
from nsm.domain.models import Classification
from nsm.engine.context import JobContext
from nsm.engine.generate import AUTO_PUBLISH_KEY
from nsm.engine.runner import run_job
from nsm.render.placeholder import PlaceholderRenderer
from nsm.store.repo import Repository
from tests.fakes import (
    FakeClassifier,
    FakeNotifier,
    FakeSource,
    FakeTextGenerator,
    load_source_items,
)

PLATFORMS = ("instagram", "threads", "naver_blog")


def test_dryrun_pipeline_writes_preview(db, clock, app_config, tmp_path: Path) -> None:
    preview = tmp_path / "preview"
    cfg = app_config.model_copy(
        update={
            "settings": app_config.settings.model_copy(update={"protect_count": 0}),
            "persona": app_config.persona.model_copy(
                update={"active_hours": parse_windows("00:00-23:59")}
            ),
        }
    )
    ctx = JobContext(
        config=cfg,
        repo=Repository(db, clock),
        clock=clock,
        notifier=FakeNotifier(),
        sources=[FakeSource("naver", load_source_items())],
        relevance_classifier=FakeClassifier(
            default=Classification(label="relevant", confidence=0.9)
        ),
        topic_classifier=FakeClassifier(default=Classification(label="핸드드립", confidence=0.8)),
        classifier_name="fake",
        text_generator=FakeTextGenerator(),
        renderer=PlaceholderRenderer(),
        adapters={p: DryRunAdapter(p, preview, clock) for p in PLATFORMS},
        media_dir=tmp_path / "media",
    )
    ctx.repo.set_setting(AUTO_PUBLISH_KEY, True)

    assert run_job("collect", ctx).ok
    assert run_job("generate", ctx).counts["created"] == 2
    assert run_job("publish", ctx).counts["published"] == 3

    day = preview / "2026-10-11"
    content_dirs = [d for d in day.iterdir() if d.is_dir()]
    assert len(content_dirs) == 1
    c = content_dirs[0]
    for p in PLATFORMS:
        files = {f.name for f in (c / p).iterdir()}
        assert any(n.startswith("slide-") and n.endswith(".png") for n in files)
    assert (c / "threads" / "post.txt").read_text("utf-8").startswith(VIRTUAL_PERSON_TEXT)
    blog = (c / "naver_blog" / "post.md").read_text("utf-8")
    assert blog.splitlines()[0].endswith(BLOG_TITLE_SUFFIX)
    assert VIRTUAL_PERSON_TEXT in blog
    assert [r["job"] for r in ctx.repo.recent_runs()] == ["publish", "generate", "collect"]
