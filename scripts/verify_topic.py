"""관심 주제 하나로 실제 한 바퀴 검증(S16): 실제 수집 → 오늘의 추천(정보·이슈·밈) → 카드 렌더.

- 사용자의 ~/.nsm은 건드리지 않고 임시 폴더(--home)에서 돈다. 키는 keyring에서만 읽고 출력하지 않는다.
- 계정 설명은 **중립**(업종·제품·인물상 비움, 관심 주제 = --topic 하나). 커피 픽스처를 쓰지 않는다.
- 밈 그림은 **사용자가 고른** 주인공 참조(--hero-ref)가 있을 때만 그린다. 없으면 대본까지만 만들고
  "캐릭터·참조 필요"로 남긴다(임의 캐릭터로 대신하지 않음).
- 결과: <home>/out/summary.json(근거 URL·확인 시각·접근 상태)과 카드 PNG.

    uv run python scripts/verify_topic.py --topic 치이카와 --home /tmp/nsm-chiikawa \
        [--info 3] [--meme 2] [--hero-name 치이카와 --hero-ref ref.png --hero-look "..."] [--cap 1.0]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _config(a):
    from nsm.config.schema import AppConfig

    raw = {
        "version": 1,
        "risk_accepted_at": "2026-10-21T09:00:00+09:00",
        "brand": {"name": "주제 검증 계정", "industry": "", "products": [], "key_messages": []},
        "persona": {
            "name": "검증",
            "handle": "@verify.test",
            "character": "",
            "tone": "짧고 친근한 존댓말",
            "banned_words": [],
            "banned_patterns": [],
            "topics": [a.topic],
            "active_hours": [{"start": "00:00", "end": "23:59"}],
            "enabled": True,
            "ad_label": False,
        },
        "settings": {
            "dry_run": True,
            "text_provider": "openai",
            "classifier": "openai",
            "browser_driver": "playwright",
            "headless_collect": True,
            "daily_cost_cap_usd": a.cap,
            "collect": {
                "naver": True,
                "google_trends": False,
                "community": False,
                "recent_period": "1m",
                "min_delay_seconds": 2.0,
                "max_delay_seconds": 4.0,
            },
            "picks": {"enabled": True, "photo_candidates": True},
            "models": {"image_quality": a.quality},
        },
        "workspace": {"info_count": a.info, "meme_count": a.meme, "allow_substitute": False},
    }
    return AppConfig.model_validate(raw)


class Recorder:
    """밈 탐색 결과를 요약에 남기려고 감싼다(쓰는 쪽 동작은 그대로)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list = []

    def discover(self, topic):
        res = self.inner.discover(topic)
        self.calls.append((topic, res))
        return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--info", type=int, default=3)
    ap.add_argument("--meme", type=int, default=2)
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--cap", type=float, default=1.0)
    ap.add_argument("--home")
    ap.add_argument("--hero-name")
    ap.add_argument(
        "--hero-ref", help="사용자가 고른 주인공 참조 이미지. 없으면 밈 그림을 그리지 않는다"
    )
    ap.add_argument("--hero-look", default="")
    a = ap.parse_args()

    from nsm.config.secrets import KeyringSecretStore

    secrets = KeyringSecretStore()
    if not secrets.get("openai_api_key"):
        print("OpenAI 키가 없습니다(docs/user-guide.md 'API 키 등록').")
        return 2
    home = Path(a.home) if a.home else Path(tempfile.mkdtemp(prefix="nsm-verify-"))
    os.environ["NSM_HOME"] = str(home)
    out = home / "out"
    out.mkdir(parents=True, exist_ok=True)

    from nsm.app.review_actions import ReviewActions
    from nsm.domain.meme_discovery import candidates
    from nsm.domain.production import Cast, CharacterRef
    from nsm.engine.picks import run_picks
    from nsm.engine.production import enqueue_production, run_images
    from nsm.engine.publish import labeled_deck
    from nsm.runtime import build_context
    from nsm.store.db import Database
    from nsm.store.picks import PicksStore
    from nsm.store.production import ProductionStore
    from nsm.store.slots import SlotStore

    cfg = _config(a)
    db = Database(home / "data.duckdb")
    summary: dict = {"topic": a.topic, "home": str(home)}
    with ExitStack() as stack:
        ctx = build_context(cfg, db, stack, secrets=secrets)
        rec = Recorder(ctx.meme_discovery) if ctx.meme_discovery else None
        ctx.meme_discovery = rec
        summary["models"] = {
            "text": cfg.settings.models.openai_text,
            "image": cfg.settings.models.image,
            "classifier": "openai(gpt 텍스트 모델, 묶음 판정)",
        }
        r = run_picks(ctx, manual=True)
        summary["run"] = {"counts": r.counts, "errors": r.errors, "skipped": r.skipped}

        # 수집된 자료(관련 판정) — 근거 URL·게시·수집 시각
        rows = db.execute(
            "SELECT title, url, source, published_at, collected_at, relevance, topic FROM source_items "
            "ORDER BY relevance DESC NULLS LAST, collected_at DESC LIMIT 40"
        )
        summary["collected"] = [
            {
                "title": t,
                "url": u,
                "source": s,
                "published_at": str(p) if p else None,
                "collected_at": str(c),
                "relevance": rel,
                "topic": tp,
            }
            for t, u, s, p, c, rel, tp in rows
        ]
        if rec and not rec.calls:  # 다시 실행해 추천이 이미 찼으면 탐색만 한 번 더(근거 목록용)
            rec.discover(a.topic)
        if rec:
            now = ctx.clock.now()
            summary["meme_discovery"] = [
                {
                    "topic": t,
                    "access": res.access,
                    "candidates": [c.to_dict() for c in candidates(res.sightings, now)],
                }
                for t, res in rec.calls
            ]

        store = PicksStore(db, ctx.clock)
        slots = SlotStore(db, ctx.clock)
        prod = ProductionStore(db, ctx.clock)
        actions = ReviewActions(ctx.repo, store, ctx.clock)
        from nsm.domain.policy import local

        today = local(ctx.clock.now(), ctx.tz).date()
        by_key = {s.key: s for s in slots.list_for_date(today)}
        summary["slots"] = [
            {"key": k, "stage": v.stage, "actual": v.actual_kind, "error": v.last_error}
            for k, v in by_key.items()
        ]
        picks_out = []
        for p in store.list(run_date=today):
            c = ctx.repo.get_content(p.content_id)
            sl = by_key.get(p.slot_key or "")
            entry = {
                "kind": p.kind,
                "reason": p.reason,
                "freshness": p.freshness,
                "topic": ctx.repo.content_topic(p.content_id),
                "slot_stage": sl.stage if sl else None,
                "slot_note": sl.last_error if sl else None,
                "plan": sl.plan if sl else None,
                "review_reason": c.review_reason,
                "slides": [
                    s.model_dump(mode="json", exclude_none=True) for s in c.generated.deck.slides
                ],
                "caption": c.generated.instagram_caption,
                "sources": [
                    {
                        "title": s.title,
                        "url": s.url,
                        "published_at": str(s.published_at),
                        "collected_at": str(s.collected_at),
                    }
                    for s in (
                        ctx.repo.get_source_item(i)
                        for i in dict.fromkeys(x.source_item_id for x in c.generated.claims)
                    )
                    if s is not None
                ],
                "photo": {"status": p.photo_status, "path": p.photo_candidate_path},
            }
            # 정보: 공식 사진 후보가 있으면 표지·마지막 장에 적용하고 렌더
            if p.kind == "info" and p.photo_status == "candidate":
                try:
                    actions.photo_use(p.content_id, ctx.media_dir, recolor=True)
                    entry["photo"]["applied"] = True
                except Exception as e:  # noqa: BLE001
                    entry["photo"]["error"] = str(e)
            # 밈: 사용자가 고른 참조가 있을 때만 그림
            if p.kind == "meme" and a.hero_ref:
                ref = Path(a.hero_ref)
                cast_dir = ctx.media_dir / "content" / p.content_id / "cast"
                cast_dir.mkdir(parents=True, exist_ok=True)
                ref_copy = cast_dir / f"hero{ref.suffix}"
                shutil.copyfile(ref, ref_copy)
                hero = CharacterRef(
                    kind="content",
                    name=a.hero_name or a.topic,
                    appearance=a.hero_look,
                    references=(str(ref_copy),),
                )
                prod.set_cast(p.content_id, Cast(hero=hero).to_json(), cfg.settings.preset)
                enqueue_production(ctx, p.content_id)
                for _ in range(4):
                    rr = run_images(ctx, max_jobs=2)
                    if rr.skipped:
                        break
                entry["image_jobs"] = [
                    {
                        "target": j.target,
                        "status": j.status,
                        "error": j.error,
                        "cost": j.cost_status,
                    }
                    for j in prod.jobs_for(p.content_id)
                ]
            c = ctx.repo.get_content(p.content_id)
            if p.kind == "info" or a.hero_ref:
                d = out / f"{p.kind}-{p.rank}"
                try:
                    entry["cards"] = [str(x) for x in ctx.renderer.render(labeled_deck(c), d)]
                    entry["overflow"] = ctx.renderer.last_overflow
                except Exception as e:  # noqa: BLE001
                    entry["render_error"] = f"{type(e).__name__}: {e}"
            picks_out.append(entry)
        summary["picks"] = picks_out
        now = ctx.clock.now()
        summary["cost_usd"] = ctx.cost_ledger.breakdown(
            now - timedelta(days=1), now + timedelta(minutes=1)
        )
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(out / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
