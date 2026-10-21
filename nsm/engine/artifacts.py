"""미리보기 판과 승인 산출물(S15 제안, ADR 0059).

- 미리보기를 그릴 때 **판(manifest)**을 남긴다: 원고 판 번호 + 지문(표기 넣은 덱 JSON, 렌더러 판 = 템플릿·스타일·캐릭터,
  덱이 가리키는 그림 파일 내용) + PNG 묶음 해시.
- 사람이 승인하면 **그 판이 지금 상태와 같은지** 확인하고(다르면 새 미리보기를 보여 주고 다시 승인받는다), 그 PNG를
  임시 폴더에 복사 → 해시 검증 → 이름 바꾸기로 굳힌다(approved/<id>/rev<N>-<판>). 캡션도 함께 저장한다.
- 게시는 굳힌 산출물만 쓴다. 산출물 판이 원고 판과 다르거나 파일이 사라졌으면 다시 그리지 않고 검토 대기로 돌린다.
- 정책 자동 승인(사람이 본 미리보기가 없음)은 게시 때 한 번 그린 것을 그 판의 산출물로 굳혀 플랫폼·재시도가 같은 PNG를 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from nsm.domain.models import CardDeck, Content
from nsm.store.production import Artifact, Manifest, ProductionStore

log = logging.getLogger(__name__)


class PreviewRequired(Exception):
    """승인하려는 원고에 지금 상태와 같은 미리보기가 없다(새로 그려 보여 주고 다시 승인받는다)."""


class ArtifactMissing(Exception):
    """승인 산출물이 없거나 원고와 판이 다르다. 게시하지 않고 검토로 돌린다."""


def _asset_paths(deck: CardDeck) -> list[str]:
    out: list[str] = []
    if deck.brand.logo_path:
        out.append(deck.brand.logo_path)
    for s in deck.slides:
        if s.background:
            out.append(s.background)
        st = getattr(s, "sticker", None)
        if st is not None:
            out.append(st.path)
        if s.kind == "products":
            out += [it.image for it in s.items if it.image]
        if s.kind == "comic":
            out += [p.image.path for p in s.panels if p.image is not None]
    return out


def deck_fingerprint(deck: CardDeck, renderer_version: str) -> str:
    h = hashlib.sha256(deck.model_dump_json().encode())
    h.update(b"|" + renderer_version.encode())
    for p in _asset_paths(deck):
        f = Path(p)
        h.update(
            b"|"
            + (hashlib.sha256(f.read_bytes()).digest() if f.is_file() else b"missing:" + p.encode())
        )
    return h.hexdigest()


def files_sha(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        h.update(p.name.encode() + b"|" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def write_manifest(
    prod: ProductionStore,
    content: Content,
    revision: int,
    deck: CardDeck,
    renderer_version: str,
    out_dir: Path,
    overflow: list[int],
) -> Manifest:
    pngs = sorted(out_dir.glob("slide-*.png"))
    m = prod.add_manifest(
        content.id,
        revision,
        deck_fingerprint(deck, renderer_version),
        str(out_dir),
        files_sha(pngs),
        ",".join(map(str, overflow)),
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": m.id,
                "revision": revision,
                "fingerprint": m.fingerprint,
                "files_sha": m.files_sha,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return m


def current_preview(
    prod: ProductionStore, content: Content, revision: int, deck: CardDeck, renderer_version: str
) -> Manifest:
    """지금 원고와 같은 판의 미리보기. 없거나 다르면 PreviewRequired."""
    m = prod.latest_manifest(content.id)
    if m is None:
        raise PreviewRequired("아직 미리보기를 보지 않았습니다")
    if m.revision != revision or m.fingerprint != deck_fingerprint(deck, renderer_version):
        raise PreviewRequired("미리보기 뒤에 원고·그림·디자인 설정이 바뀌었습니다")
    pngs = sorted(Path(m.dir).glob("slide-*.png"))
    if not pngs or files_sha(pngs) != m.files_sha:
        raise PreviewRequired("미리보기 파일이 바뀌었거나 사라졌습니다")
    if m.overflow:
        raise PreviewRequired(f"미리보기 {m.overflow}장에 글이 넘칩니다. 문구를 줄여 주세요")
    return m


def freeze(
    prod: ProductionStore,
    media_dir: Path,
    content: Content,
    revision: int,
    src_pngs: list[Path],
    expect_sha: str,
    manifest_id: str | None,
    by: str,
) -> Artifact:
    """PNG를 임시 폴더에 복사 → 해시 검증 → 이름 바꾸기. 캡션도 함께. 실패하면 남는 것이 없다."""
    base = media_dir / "approved" / content.id
    base.mkdir(parents=True, exist_ok=True)
    tag = f"rev{revision}-{(manifest_id or 'auto')[-8:]}"
    tmp = base / f".tmp-{tag}"
    final = base / tag
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        for p in src_pngs:
            shutil.copyfile(p, tmp / p.name)
        copied = sorted(tmp.glob("slide-*.png"))
        if files_sha(copied) != expect_sha:
            raise ArtifactMissing("복사한 카드 파일의 해시가 미리보기와 다릅니다")
        g = content.generated
        (tmp / "captions.json").write_text(
            json.dumps(
                {
                    "instagram": g.instagram_caption,
                    "threads": g.threads_text,
                    "blog_title": g.blog_title,
                    "blog_markdown": g.blog_markdown,
                    "revision": revision,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    prod.add_artifact(content.id, revision, manifest_id, str(final), expect_sha, by)
    return prod.latest_artifact(content.id)  # type: ignore[return-value]


def artifact_pngs(prod: ProductionStore, content: Content, revision: int) -> list[Path] | None:
    """게시용 산출물 PNG. 산출물이 없으면 None(정책 승인·예전 원고). 있는데 판이 다르거나 깨졌으면 ArtifactMissing."""
    a = prod.latest_artifact(content.id)
    if a is None:
        return None
    if a.revision != revision:
        raise ArtifactMissing(f"승인한 판({a.revision})과 원고 판({revision})이 다릅니다")
    pngs = sorted(Path(a.dir).glob("slide-*.png"))
    if not pngs or files_sha(pngs) != a.files_sha:
        raise ArtifactMissing("승인 산출물 파일이 사라졌거나 바뀌었습니다")
    return pngs


def captions(prod: ProductionStore, content_id: str) -> dict[str, Any] | None:
    a = prod.latest_artifact(content_id)
    if a is None:
        return None
    f = Path(a.dir) / "captions.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def migrate_approved(ctx) -> None:
    """S15 배포 때 한 번: 산출물 없이 승인돼 있는 원고 처리.
    - 사람이 승인했고 게시용 렌더(media/cards/<id>)가 있으면 그것을 그 판의 산출물로 채택
    - 사람이 승인했는데 렌더가 없으면 검토 대기로 돌리고 알림(재렌더해서 몰래 게시하지 않음)
    - 정책 자동 승인은 그대로(게시 때 한 번 그려 굳힌다)
    """
    from nsm.store.production import ProductionStore

    prod = ProductionStore(ctx.repo.db, ctx.clock)
    rows = ctx.repo.db.execute(
        "SELECT id, approved_by FROM contents WHERE status = 'approved' AND id NOT IN "
        "(SELECT content_id FROM approved_artifacts)"
    )
    reverted = 0
    for cid, by in rows:
        if by != "human":
            continue
        content = ctx.repo.get_content(cid)
        if content is None:
            continue
        cards = sorted((ctx.media_dir / "cards" / cid).glob("slide-*.png"))
        if cards and len(cards) == len(content.generated.deck.slides):
            freeze(
                prod,
                ctx.media_dir,
                content,
                ctx.repo.content_revision(cid),
                cards,
                files_sha(cards),
                None,
                "legacy",
            )
            continue
        ctx.repo.transition_content(
            cid, "pending_review", reason="승인 산출물이 없어 미리보기 확인 뒤 다시 승인 필요(S15)"
        )
        reverted += 1
    if reverted:
        ctx.notifier.notify(
            "다시 승인 필요",
            f"승인된 원고 {reverted}건에 게시용 카드가 없어 검토 대기로 돌렸습니다. 미리보기를 보고 다시 승인하세요",
            "warn",
        )
