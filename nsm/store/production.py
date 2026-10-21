"""그림 제작 저장소(S15 제안, ADR 0058·0059): 출연진·장면, 영속 이미지 작업, 그림 이력, 미리보기 판, 승인 산출물.

- 이미지 작업은 **호출 전에** 행이 있다(queued → claimed → sent → succeeded|succeeded_stale|failed|unknown).
  재시작하면 claimed(아직 안 보냄)은 queued로, sent(보냈는데 결과 모름)는 unknown으로 돌린다.
- 결과 반영은 한 트랜잭션: 그림 이력(candidate) 추가 + (지금 슬롯 spec과 같으면) 활성화·작업 succeeded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nsm.domain.clock import new_id
from nsm.ports import Clock
from nsm.store.db import Database, from_db, to_db

IN_FLIGHT = ("queued", "claimed", "sent")
JOB_COLS = (
    "id, content_id, target, role, spec_hash, spec_json, depends_on, status, attempts, next_attempt_at, "
    "run_id, request_id, error, preset, cost_status, result_image_id, created_at, updated_at, result_path"
)
IMG_COLS = (
    "id, content_id, target, role, source, path, sha256, provider, model, prompt, references_json, "
    "spec_hash, job_id, state, cost_usd, cost_status, created_at"
)


@dataclass
class ImageJob:
    id: str
    content_id: str | None
    target: str
    role: str
    spec_hash: str
    spec: dict[str, Any]
    depends_on: str | None
    status: str
    attempts: int
    next_attempt_at: datetime | None
    run_id: str | None
    request_id: str | None
    error: str | None
    preset: str | None
    cost_status: str | None
    result_image_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    result_path: str | None = None


@dataclass
class ContentImage:
    id: str
    content_id: str
    target: str
    role: str
    source: str
    path: str
    sha256: str
    provider: str | None
    model: str | None
    prompt: str | None
    references: list[str]
    spec_hash: str | None
    job_id: str | None
    state: str
    cost_usd: float | None
    cost_status: str | None
    created_at: datetime | None


@dataclass
class Manifest:
    id: str
    content_id: str
    revision: int
    fingerprint: str
    dir: str
    files_sha: str
    overflow: str
    created_at: datetime | None


@dataclass
class Artifact:
    content_id: str
    revision: int
    manifest_id: str | None
    dir: str
    files_sha: str
    approved_by: str | None
    created_at: datetime | None


class ProductionStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def _now(self) -> datetime:
        return to_db(self.clock.now())  # type: ignore[return-value]

    # ------------------------------------------------------------ 출연진·장면
    def get_production(self, cid: str) -> tuple[str | None, dict[str, str]]:
        rows = self.db.execute(
            "SELECT cast_json, scenes_json FROM content_production WHERE content_id = ?", [cid]
        )
        if not rows:
            return None, {}
        return rows[0][0], json.loads(rows[0][1]) if rows[0][1] else {}

    def set_cast(self, cid: str, cast_json: str, preset: str | None) -> None:
        self.db.execute(
            "INSERT INTO content_production (content_id, cast_json, scenes_json, preset, updated_at) "
            "VALUES (?, ?, NULL, ?, ?) ON CONFLICT (content_id) DO UPDATE SET cast_json = excluded.cast_json, "
            "preset = excluded.preset, updated_at = excluded.updated_at",
            [cid, cast_json, preset, self._now()],
        )

    def set_scene(self, cid: str, target: str, scene: str) -> None:
        _cast, scenes = self.get_production(cid)
        scenes[target] = scene
        self.db.execute(
            "INSERT INTO content_production (content_id, cast_json, scenes_json, preset, updated_at) "
            "VALUES (?, NULL, ?, NULL, ?) ON CONFLICT (content_id) DO UPDATE SET scenes_json = excluded.scenes_json, "
            "updated_at = excluded.updated_at",
            [cid, json.dumps(scenes, ensure_ascii=False), self._now()],
        )

    # ------------------------------------------------------------ 작업
    @staticmethod
    def _job(r: tuple) -> ImageJob:
        return ImageJob(
            id=r[0],
            content_id=r[1],
            target=r[2],
            role=r[3],
            spec_hash=r[4],
            spec=json.loads(r[5]) if r[5] else {},
            depends_on=r[6],
            status=r[7],
            attempts=int(r[8] or 0),
            next_attempt_at=from_db(r[9]),
            run_id=r[10],
            request_id=r[11],
            error=r[12],
            preset=r[13],
            cost_status=r[14],
            result_image_id=r[15],
            created_at=from_db(r[16]),
            updated_at=from_db(r[17]),
            result_path=r[18],
        )

    def enqueue(
        self,
        content_id: str | None,
        target: str,
        role: str,
        spec_hash: str,
        spec: dict[str, Any],
        preset: str | None,
        depends_on: str | None = None,
    ) -> ImageJob | None:
        """같은 대상·같은 spec 작업이 이미 진행 중이면 만들지 않는다(중복 유료 호출 방지). 만들었으면 작업."""
        with self.db.transaction() as cur:
            cur.execute(
                "SELECT id FROM image_jobs WHERE coalesce(content_id, '') = coalesce(?, '') AND target = ? "
                "AND spec_hash = ? AND status IN ('queued', 'claimed', 'sent')",
                [content_id, target, spec_hash],
            )
            if cur.fetchall():
                return None
            jid = new_id()
            now = self._now()
            cur.execute(
                f"INSERT INTO image_jobs ({JOB_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, NULL, "
                "NULL, ?, NULL, NULL, ?, ?, NULL)",
                [
                    jid,
                    content_id,
                    target,
                    role,
                    spec_hash,
                    json.dumps(spec, ensure_ascii=False),
                    depends_on,
                    preset,
                    now,
                    now,
                ],
            )
        return self.job(jid)

    def job(self, jid: str) -> ImageJob | None:
        rows = self.db.execute(f"SELECT {JOB_COLS} FROM image_jobs WHERE id = ?", [jid])
        return self._job(rows[0]) if rows else None

    def jobs_for(self, cid: str) -> list[ImageJob]:
        rows = self.db.execute(
            f"SELECT {JOB_COLS} FROM image_jobs WHERE content_id = ? ORDER BY created_at, id", [cid]
        )
        return [self._job(r) for r in rows]

    def latest_jobs(self, cid: str) -> dict[str, ImageJob]:
        """대상별 가장 최근 작업."""
        out: dict[str, ImageJob] = {}
        for j in self.jobs_for(cid):
            out[j.target] = j
        return out

    def next_due(self, now: datetime, preset: str | None) -> ImageJob | None:
        rows = self.db.execute(
            f"SELECT {JOB_COLS} FROM image_jobs WHERE status = 'queued' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at, id",
            [to_db(now)],
        )
        for r in rows:
            j = self._job(r)
            if preset is None or j.preset in (None, preset):
                return j
        return None

    def claim(self, jid: str, run_id: str) -> bool:
        return bool(
            self.db.execute(
                "UPDATE image_jobs SET status = 'claimed', run_id = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued' RETURNING id",
                [run_id, self._now(), jid],
            )
        )

    def mark_sent(self, jid: str) -> None:
        self.db.execute(
            "UPDATE image_jobs SET status = 'sent', attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = 'claimed'",
            [self._now(), jid],
        )

    def finish(
        self,
        jid: str,
        status: str,
        *,
        error: str | None = None,
        request_id: str | None = None,
        next_attempt_at: datetime | None = None,
        cost_status: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE image_jobs SET status = ?, error = ?, request_id = coalesce(?, request_id), "
            "next_attempt_at = ?, cost_status = coalesce(?, cost_status), updated_at = ? WHERE id = ?",
            [
                status,
                (error or "")[:300] or None,
                request_id,
                to_db(next_attempt_at),
                cost_status,
                self._now(),
                jid,
            ],
        )

    def requeue(self, jid: str) -> bool:
        """사람이 누른 '다시 요청'(failed·unknown·cancelled → queued). 미확인 비용은 별도로 풀거나 남긴다."""
        return bool(
            self.db.execute(
                "UPDATE image_jobs SET status = 'queued', error = NULL, next_attempt_at = NULL, updated_at = ? "
                "WHERE id = ? AND status IN ('failed', 'unknown', 'cancelled') RETURNING id",
                [self._now(), jid],
            )
        )

    def save_result(
        self,
        jid: str,
        image_id: str | None,
        path: str,
        request_id: str | None,
        cost_status: str | None,
        cur=None,
    ) -> None:
        """유료 결과 보존(적용 전): status=saved, 파일 경로·이력 id·요청 id."""
        sql = (
            "UPDATE image_jobs SET status = 'saved', result_image_id = ?, result_path = ?, "
            "request_id = coalesce(?, request_id), cost_status = ?, updated_at = ? WHERE id = ?"
        )
        args = [image_id, path, request_id or None, cost_status, self._now(), jid]
        (cur.execute(sql, args) if cur is not None else self.db.execute(sql, args))

    def recover(self) -> tuple[int, int]:
        """재시작: claimed(아직 안 보냄) → queued, sent(보냈는데 결과 모름) → unknown(자동 재시도 금지)."""
        a = self.db.execute(
            "UPDATE image_jobs SET status = 'queued', updated_at = ? WHERE status = 'claimed' RETURNING id",
            [self._now()],
        )
        b = self.db.execute(
            "UPDATE image_jobs SET status = 'unknown', error = '응답을 받기 전에 프로그램이 멈췄습니다(과금 여부 불명)', "
            "cost_status = 'unknown', updated_at = ? WHERE status = 'sent' RETURNING id",
            [self._now()],
        )
        # 결과는 저장했는데 적용 전에 멈춤: 다시 부르지 않고 적용만 다시(사람이 버튼, 또는 다음 images 잡)
        self.db.execute(
            "UPDATE image_jobs SET status = 'apply_failed', error = '적용 전에 멈춤(그림 보존, 다시 적용 가능)', "
            "updated_at = ? WHERE status = 'saved'",
            [self._now()],
        )
        return len(a), len(b)

    # ------------------------------------------------------------ 그림 이력
    @staticmethod
    def _img(r: tuple) -> ContentImage:
        return ContentImage(
            id=r[0],
            content_id=r[1],
            target=r[2],
            role=r[3],
            source=r[4],
            path=r[5],
            sha256=r[6],
            provider=r[7],
            model=r[8],
            prompt=r[9],
            references=json.loads(r[10]) if r[10] else [],
            spec_hash=r[11],
            job_id=r[12],
            state=r[13],
            cost_usd=r[14],
            cost_status=r[15],
            created_at=from_db(r[16]),
        )

    def images(self, cid: str, target: str | None = None) -> list[ContentImage]:
        sql = f"SELECT {IMG_COLS} FROM content_images WHERE content_id = ?"
        params: list[Any] = [cid]
        if target:
            sql += " AND target = ?"
            params.append(target)
        rows = self.db.execute(sql + " ORDER BY created_at DESC, id DESC", params)
        return [self._img(r) for r in rows]

    def image(self, image_id: str) -> ContentImage | None:
        rows = self.db.execute(f"SELECT {IMG_COLS} FROM content_images WHERE id = ?", [image_id])
        return self._img(rows[0]) if rows else None

    def active(self, cid: str, target: str) -> ContentImage | None:
        rows = self.db.execute(
            f"SELECT {IMG_COLS} FROM content_images WHERE content_id = ? AND target = ? AND state = 'active'",
            [cid, target],
        )
        return self._img(rows[0]) if rows else None

    def ai_paths(self, cid: str) -> set[str]:
        rows = self.db.execute(
            "SELECT path FROM content_images WHERE content_id = ? AND source = 'ai'", [cid]
        )
        return {r[0] for r in rows}

    def add_image(
        self,
        cur,
        *,
        cid: str,
        target: str,
        role: str,
        source: str,
        path: str,
        sha: str,
        state: str,
        provider: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        references: list[str] | None = None,
        spec_hash: str | None = None,
        job_id: str | None = None,
        cost_usd: float | None = None,
        cost_status: str | None = None,
    ) -> str:
        iid = new_id()
        if state == "active":
            cur.execute(
                "UPDATE content_images SET state = 'archived' WHERE content_id = ? AND target = ? AND state = 'active'",
                [cid, target],
            )
        cur.execute(
            f"INSERT INTO content_images ({IMG_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                iid,
                cid,
                target,
                role,
                source,
                path,
                sha,
                provider,
                model,
                prompt,
                json.dumps(references or [], ensure_ascii=False),
                spec_hash,
                job_id,
                state,
                cost_usd,
                cost_status,
                self._now(),
            ],
        )
        return iid

    def activate(self, cur, cid: str, target: str, image_id: str) -> None:
        cur.execute(
            "UPDATE content_images SET state = 'archived' WHERE content_id = ? AND target = ? AND state = 'active'",
            [cid, target],
        )
        cur.execute(
            "UPDATE content_images SET state = 'active' WHERE id = ? AND content_id = ? AND target = ?",
            [image_id, cid, target],
        )

    # ------------------------------------------------------------ 미리보기 판·승인 산출물
    @staticmethod
    def _man(r: tuple) -> Manifest:
        return Manifest(r[0], r[1], int(r[2]), r[3], r[4], r[5], r[6] or "", from_db(r[7]))

    def add_manifest(
        self, cid: str, revision: int, fingerprint: str, dir_: str, files_sha: str, overflow: str
    ) -> Manifest:
        mid = new_id()
        self.db.execute(
            "INSERT INTO preview_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [mid, cid, revision, fingerprint, dir_, files_sha, overflow, self._now()],
        )
        return self.manifest(mid)  # type: ignore[return-value]

    def manifest(self, mid: str) -> Manifest | None:
        rows = self.db.execute("SELECT * FROM preview_manifests WHERE id = ?", [mid])
        return self._man(rows[0]) if rows else None

    def latest_manifest(self, cid: str) -> Manifest | None:
        rows = self.db.execute(
            "SELECT * FROM preview_manifests WHERE content_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            [cid],
        )
        return self._man(rows[0]) if rows else None

    @staticmethod
    def _art(r: tuple) -> Artifact:
        return Artifact(r[0], int(r[1]), r[2], r[3], r[4], r[5], from_db(r[6]))

    def add_artifact(
        self, cid: str, revision: int, manifest_id: str | None, dir_: str, files_sha: str, by: str
    ) -> None:
        self.db.execute(
            "INSERT INTO approved_artifacts VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (content_id, revision) "
            "DO UPDATE SET manifest_id = excluded.manifest_id, dir = excluded.dir, files_sha = excluded.files_sha, "
            "approved_by = excluded.approved_by, created_at = excluded.created_at",
            [cid, revision, manifest_id, dir_, files_sha, by, self._now()],
        )

    def latest_artifact(self, cid: str) -> Artifact | None:
        rows = self.db.execute(
            "SELECT * FROM approved_artifacts WHERE content_id = ? ORDER BY revision DESC, created_at DESC LIMIT 1",
            [cid],
        )
        return self._art(rows[0]) if rows else None

    def drop_artifact(self, cid: str, revision: int) -> None:
        self.db.execute(
            "DELETE FROM approved_artifacts WHERE content_id = ? AND revision = ?", [cid, revision]
        )
