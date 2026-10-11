"""드라이런 플랫폼 어댑터 (ADR 0005).

- publish: 실제로 올리지 않고 `preview/YYYY-MM-DD/<content_id>/<platform>/`에 글·이미지를 저장한다.
- 댓글·지표: 게시 시각을 기준으로 정해진 규칙에 따라 가짜 데이터를 만든다(같은 입력 → 같은 결과).
- 답글·숨김: 미리보기 폴더의 `actions.log`에 기록한다.
실계정 어댑터와 같은 포트를 구현하므로 엔진은 차이를 모른다.
"""

from __future__ import annotations

import functools
import hashlib
import json
import shutil
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

from nsm.domain.models import (
    AccountMetrics,
    Comment,
    HealthStatus,
    Platform,
    PostMetrics,
    PublishResult,
    RenderedPost,
)
from nsm.ports import CAPABILITIES, Clock

COMMENT_INTERVAL = timedelta(minutes=20)


@functools.cache
def _load_samples() -> list[dict]:
    text = resources.files("nsm.adapters.platforms.data").joinpath("dryrun_comments.json")
    return json.loads(text.read_text(encoding="utf-8"))


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class DryRunAdapter:
    def __init__(
        self,
        platform: Platform,
        preview_dir: Path,
        clock: Clock,
        samples: list[dict] | None = None,
        tz: str = "Asia/Seoul",
    ) -> None:
        self.platform = platform
        self.preview_dir = preview_dir
        self.clock = clock
        self.samples = samples if samples is not None else _load_samples()
        self.tz = ZoneInfo(tz)
        self._index_path = preview_dir / f".dryrun-{platform}.json"
        self._index_cache: dict[str, dict] | None = None

    # ---------------------------------------------------------------- index

    def _index(self) -> dict[str, dict]:
        if self._index_cache is None:
            self._index_cache = (
                json.loads(self._index_path.read_text(encoding="utf-8"))
                if self._index_path.exists()
                else {}
            )
        return self._index_cache

    def _save_index(self, idx: dict[str, dict]) -> None:
        self._index_cache = idx
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

    def _log(self, line: str) -> None:
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        with (self.preview_dir / "actions.log").open("a", encoding="utf-8") as f:
            f.write(
                f"{self.clock.now().astimezone(self.tz).isoformat()} [{self.platform}] {line}\n"
            )

    # ---------------------------------------------------------------- port

    def capabilities(self) -> set[str]:
        return set(CAPABILITIES)

    def health(self) -> HealthStatus:
        return HealthStatus(logged_in=True, detail="드라이런")

    def publish(self, post: RenderedPost) -> PublishResult:
        now = self.clock.now()
        day = now.astimezone(self.tz).strftime("%Y-%m-%d")  # 사용자가 보는 현지 날짜 폴더
        out = self.preview_dir / day / post.content_id / self.platform
        out.mkdir(parents=True, exist_ok=True)
        body = f"# {post.title}\n\n{post.text}" if post.title else post.text
        (out / ("post.md" if self.platform == "naver_blog" else "post.txt")).write_text(
            body, encoding="utf-8"
        )
        for img in post.images:
            if img.exists():
                shutil.copy2(img, out / img.name)
        ext = f"dryrun-{self.platform}-{post.content_id}"
        idx = self._index()
        idx[ext] = {"published_at": now.isoformat(), "dir": str(out)}
        self._save_index(idx)
        self._log(f"게시 {ext} → {out}")
        return PublishResult(external_id=ext, url=out.as_uri())

    def fetch_new_comments(
        self, post_external_ids: list[str], since: datetime | None
    ) -> list[Comment]:
        idx = self._index()
        now = self.clock.now()
        out: list[Comment] = []
        for pid in post_external_ids:
            meta = idx.get(pid)
            if not meta:
                continue
            published = datetime.fromisoformat(meta["published_at"])
            # 게시물마다 샘플의 일부(최대 5개)를 정해진 순서로 단다
            start = _seed(pid) % max(1, len(self.samples))
            picks = [
                self.samples[(start + i) % len(self.samples)]
                for i in range(min(5, len(self.samples)))
            ]
            for i, s in enumerate(picks):
                created = published + COMMENT_INTERVAL * (i + 1)
                if created > now or (since is not None and created <= since):
                    continue
                out.append(
                    Comment(
                        platform=self.platform,
                        external_id=f"{pid}-c{i}",
                        post_external_id=pid,
                        author_id=s["author_id"],
                        author_name=s["author_name"],
                        text=s["text"],
                        created_at=created,
                    )
                )
        return out

    def reply(self, comment: Comment, text: str) -> str:
        self._log(f"답글 {comment.external_id} ({comment.author_name}): {text}")
        return f"{comment.external_id}-reply"

    def hide_comment(self, comment: Comment) -> None:
        self._log(f"숨김 {comment.external_id} ({comment.author_name}): {comment.text}")

    def read_post_metrics(self, post_external_id: str) -> PostMetrics:
        meta = self._index().get(post_external_id)
        now = self.clock.now()
        hours = 0.0
        if meta:
            hours = max(
                0.0, (now - datetime.fromisoformat(meta["published_at"])).total_seconds() / 3600
            )
        base = 200 + _seed(post_external_id) % 800
        growth = min(1.0, hours / 72)  # 3일에 걸쳐 차오른다
        views = int(base * (0.2 + 0.8 * growth))
        return PostMetrics(
            platform=self.platform,
            post_external_id=post_external_id,
            captured_at=now,
            views=views,
            reach=int(views * 0.8),
            impressions=int(views * 1.2),
            likes=int(views * 0.06),
            comments=int(views * 0.01),
            saves=int(views * 0.02) if self.platform == "instagram" else None,
            shares=int(views * 0.01) if self.platform != "naver_blog" else None,
        )

    def read_account_metrics(self) -> AccountMetrics:
        return AccountMetrics(
            platform=self.platform,
            captured_at=self.clock.now(),
            followers=100 + 7 * len(self._index()),
        )
