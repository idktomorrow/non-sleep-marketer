"""검토 동작 한곳(S11 제안, ADR 0051): 승인·폐기·게시 취소가 추천 원고(picks)에도 같은 뜻으로 동작하게.

- 추천 원고를 승인하면 게시 레코드까지 한 트랜잭션으로 만든다("승인됐는데 안 올라가는" 상태 방지)
- 폐기하면 자료를 풀고 추천은 dismissed(지문은 남겨 재추천 방지)
- 보관한 지 오래된 원고는 날짜·가격·행사 종료를 다시 확인했다고 표시해야 게시로 보낼 수 있다
"""

from __future__ import annotations

import secrets
import shutil
from datetime import timedelta
from pathlib import Path

from nsm.app.card_editor import apply_photo_colors, clear_rendered, set_background, upload_dir
from nsm.domain.models import GeneratedContent, Persona, Platform
from nsm.domain.policy import find_banned
from nsm.engine.generate import all_texts
from nsm.ports import Clock
from nsm.store.picks import PicksStore
from nsm.store.repo import InvalidTransitionError, Repository

RECHECK_AFTER = timedelta(hours=48)  # 만든 지 이보다 오래된 추천은 게시 전 재확인 필요
RECHECK_VALID = timedelta(hours=24)  # 재확인 표시는 이 시간 동안 유효


class ActionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class PreviewNeeded(ActionError):
    """지금 원고와 같은 미리보기가 없어 승인할 수 없다(S15). 라우트는 새 미리보기를 그리고 다시 승인받는다."""

    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


class ApprovalGate:
    """사람 승인 = 사용자가 본 미리보기 판을 그대로 굳히기(S15, ADR 0059). 렌더러가 있을 때만 쓴다."""

    def __init__(self, prod, media_dir: Path, renderer_version) -> None:
        self.prod = prod
        self.media_dir = media_dir
        self._version = renderer_version  # 렌더러 판(템플릿·스타일·캐릭터)을 돌려주는 함수

    def check(self, repo: Repository, content):
        from nsm.engine.artifacts import PreviewRequired, current_preview
        from nsm.engine.publish import labeled_deck

        try:
            return current_preview(
                self.prod,
                content,
                repo.content_revision(content.id),
                labeled_deck(content),
                self._version(),
            )
        except PreviewRequired as e:
            raise PreviewNeeded(f"{e}. 새 미리보기를 확인한 뒤 다시 승인해 주세요") from e

    def freeze(self, repo: Repository, content, manifest):
        """굳히고 (판 번호, 같은 판의 이전 산출물, 새 폴더)를 돌려준다(실패 시 되돌리기용)."""
        from nsm.engine.artifacts import freeze

        revision = manifest.revision
        previous = self.prod.latest_artifact(content.id)
        if previous is not None and previous.revision != revision:
            previous = None
        pngs = sorted(Path(manifest.dir).glob("slide-*.png"))
        art = freeze(
            self.prod,
            self.media_dir,
            content,
            revision,
            pngs,
            manifest.files_sha,
            manifest.id,
            "human",
        )
        return revision, previous, art.dir

    def rollback(self, cid: str, frozen) -> None:
        """승인이 실패했을 때: 같은 판의 이전 산출물이 있었으면 되살리고, 이번에 만든 폴더는 지운다(S15 리뷰)."""
        import shutil

        revision, previous, new_dir = frozen
        if previous is not None:
            self.prod.add_artifact(
                cid,
                revision,
                previous.manifest_id,
                previous.dir,
                previous.files_sha,
                previous.approved_by or "human",
            )
        else:
            self.prod.drop_artifact(cid, revision)
        if previous is None or Path(previous.dir) != Path(new_dir):
            shutil.rmtree(new_dir, ignore_errors=True)


class ReviewActions:
    def __init__(
        self,
        repo: Repository,
        picks: PicksStore | None,
        clock: Clock,
        gate: ApprovalGate | None = None,
    ) -> None:
        self.repo = repo
        self.picks = picks
        self.clock = clock
        self.gate = gate

    def _content(self, cid: str):
        c = self.repo.get_content(cid)
        if c is None:
            raise ActionError(404, "콘텐츠가 없습니다")
        return c

    def _pick(self, cid: str):
        return self.picks.get(cid) if self.picks is not None else None

    def _publishing(self, cid: str) -> bool:
        return any(p.status == "publishing" for p in self.repo.publications_for_content(cid))

    # ------------------------------------------------------------ 승인·폐기·취소

    def _production_gate(self, cid: str) -> None:
        """그림 제작이 끝나지 않은 추천 원고(밈)는 게시로 보내지 않는다(S14). 슬롯이 없으면 통과."""
        from nsm.store.slots import SlotStore

        sl = SlotStore(self.repo.db, self.clock).by_content(cid)
        if sl is not None and sl.stage in (
            "images_pending",
            "producing",
            "failed_image",
            "unknown_image",
        ):
            raise ActionError(
                409, "그림 제작이 끝나지 않은 원고는 게시할 수 없습니다(추천함에서 제작·재시도)"
            )

    def approve(self, cid: str, *, persona: Persona, platforms: list[Platform]) -> None:
        c = self._content(cid)
        banned = find_banned(all_texts(c.generated), persona)
        if banned:
            # 금칙어가 든 콘텐츠는 사람도 그대로 승인할 수 없다: 고친 뒤 승인
            raise ActionError(
                409, f"금칙어·금칙 패턴이 있어 승인할 수 없습니다: {', '.join(banned)}"
            )
        self._production_gate(cid)
        pick = self._pick(cid)
        if pick is not None:
            self._recheck_gate(c.created_at, pick.rechecked_at)
        # 사람이 본 미리보기 판을 굳힌 뒤에야 승인한다(S15). 승인이 실패하면 굳힌 것을 지운다
        frozen = None
        expected: int | None = None
        if self.gate is not None:
            manifest = self.gate.check(self.repo, c)
            frozen = self.gate.freeze(self.repo, c, manifest)
            expected = (
                manifest.revision
            )  # 굳힌 판일 때만 승인(그 사이 그림이 바뀌면 거절, S15 리뷰)
        try:
            if pick is None:
                try:
                    self.repo.approve_human(cid, expected)
                except InvalidTransitionError as e:
                    raise ActionError(409, str(e)) from e
                self.repo.requeue_abandoned(cid)
                return
            try:
                self.picks.queue(cid, platforms, expected)  # type: ignore[union-attr]
            except InvalidTransitionError as e:
                raise ActionError(409, str(e)) from e
        except Exception:
            if frozen is not None and self.gate is not None:
                self.gate.rollback(cid, frozen)
            raise

    def _recheck_gate(self, created_at, rechecked_at) -> None:
        if not self.needs_recheck(created_at, rechecked_at, self.clock.now()):
            return
        raise ActionError(
            409,
            "보관한 지 48시간이 넘은 원고입니다. 자료 날짜·가격·행사 종료 여부를 다시 확인한 뒤 "
            "'다시 확인했어요'를 누르고 게시로 보내세요",
        )

    def discard(self, cid: str) -> bool:
        """폐기. 게시 대기 중이던 원고였으면 True(돌아갈 화면을 고르는 데 쓴다)."""
        c = self._content(cid)
        if self._publishing(cid):
            raise ActionError(409, "지금 올리는 중입니다. 잠시 뒤 다시 시도하세요")
        was_queued = c.status == "approved"
        try:
            if self._pick(cid) is not None:
                self.picks.dismiss(cid)  # type: ignore[union-attr]
            else:
                self.repo.transition_content(cid, "discarded", reason="사람이 폐기")
        except InvalidTransitionError as e:
            raise ActionError(409, str(e)) from e
        return was_queued

    def unapprove(self, cid: str) -> None:
        c = self._content(cid)
        if self._publishing(cid):
            raise ActionError(409, "지금 올리는 중입니다. 잠시 뒤 다시 시도하세요")
        if c.status != "approved":
            raise ActionError(409, "게시 대기 중인 원고만 되돌릴 수 있습니다")
        try:
            if self._pick(cid) is not None:
                self.picks.unqueue(cid)  # type: ignore[union-attr]
            else:
                self.repo.transition_content(
                    cid, "pending_review", reason="사람이 게시 대기에서 되돌림"
                )
        except InvalidTransitionError as e:
            raise ActionError(409, str(e)) from e

    # ------------------------------------------------------------ 추천 전용

    def _need_pick(self, cid: str):
        self._content(cid)
        pick = self._pick(cid)
        if pick is None:
            raise ActionError(404, "추천 원고가 아닙니다")
        return pick

    def save_for_later(self, cid: str) -> None:
        self._need_pick(cid)
        if not self.picks.set_state(cid, ("ready",), "saved"):  # type: ignore[union-attr]
            raise ActionError(409, "오늘의 추천 상태인 원고만 보관할 수 있습니다")

    def recheck(self, cid: str) -> None:
        self._need_pick(cid)
        self.picks.set_rechecked(cid, self.clock.now())  # type: ignore[union-attr]

    def invalidate_render(self, cid: str) -> None:
        """글·배경을 고쳤으면 미리보기 확인은 무효(넘침이 다시 생길 수 있다). 추천 원고가 아니면 아무것도 안 한다."""
        if self.picks is not None and self.picks.get(cid) is not None:
            self.picks.set_render_checked(cid, None)

    @staticmethod
    def needs_recheck(created_at, rechecked_at, now) -> bool:
        """만든 지 RECHECK_AFTER가 지났고, 재확인이 없거나 RECHECK_VALID보다 오래됐다."""
        return now - created_at > RECHECK_AFTER and (
            rechecked_at is None or now - rechecked_at > RECHECK_VALID
        )

    def mark_render_checked(self, cid: str, overflow: list[int]) -> None:
        """미리보기 렌더 결과를 추천 상태에 반영: 넘침 없음이면 확인 시각, 있으면 비움."""
        if self.picks is None or self.picks.get(cid) is None:
            return
        self.picks.set_render_checked(cid, None if overflow else self.clock.now())

    def photo_use(self, cid: str, media_dir: Path, recolor: bool = False) -> GeneratedContent:
        """사진 후보를 표지와 마지막 장 배경으로. 검토 대기 중일 때만. recolor면 사진 색을 덱 색으로(S14)."""
        c = self._content(cid)
        pick = self._need_pick(cid)
        if c.status != "pending_review":
            raise ActionError(409, "검토 대기 중인 콘텐츠만 고칠 수 있습니다")
        src = Path(pick.photo_candidate_path) if pick.photo_candidate_path else None
        if pick.photo_status != "candidate" or src is None or not src.is_file():
            raise ActionError(409, "쓸 수 있는 사진 후보가 없습니다")
        out = upload_dir(media_dir, cid)
        out.mkdir(parents=True, exist_ok=True)
        # 표지와 마지막 장에 **각각 다른 파일**로 복사한다: 한 장의 배경을 지우면 올린 파일을 지우므로(drop_old_upload),
        # 같은 파일을 두 장이 가리키면 다른 장의 배경이 사라진다(S13 리뷰)
        dest = out / f"slide-01-{secrets.token_hex(4)}{src.suffix}"
        shutil.copyfile(src, dest)
        g = set_background(c.generated, 0, str(dest), media_dir)
        last = len(g.deck.slides) - 1
        dest_last = out / f"slide-{last + 1:02d}-{secrets.token_hex(4)}{src.suffix}"
        shutil.copyfile(src, dest_last)
        g = set_background(g, last, str(dest_last), media_dir)
        if recolor:
            g = apply_photo_colors(g, dest)
        try:
            self.repo.update_generated(cid, g)
        except InvalidTransitionError as e:
            raise ActionError(409, str(e)) from e
        clear_rendered(media_dir, cid)
        self.picks.set_photo(cid, "applied")  # type: ignore[union-attr]
        return g

    def photo_drop(self, cid: str) -> None:
        pick = self._need_pick(cid)
        if pick.photo_status != "candidate":
            raise ActionError(409, "확인 대기 중인 사진 후보가 없습니다")
        self.picks.set_photo(cid, "dropped")  # type: ignore[union-attr]
