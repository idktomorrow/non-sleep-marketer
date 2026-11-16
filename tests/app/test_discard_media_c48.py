"""폐기하면 그 원고의 카드·미리보기·올린 배경 폴더를 지운다 (배치 C48)."""

from __future__ import annotations

import logging

from nsm.domain.clock import new_id
from nsm.engine import media_cleanup as M
from tests.app.test_dashboard import _post, env  # noqa: F401


def _copy(env, status: str = "pending_review") -> str:  # noqa: F811
    cid = new_id()
    c = env["repo"].get_content("c1")
    env["repo"].insert_content(c.model_copy(update={"id": cid, "status": status}))
    return cid


def _fill(env, cid: str) -> tuple:  # noqa: F811
    dirs = M.content_media_dirs(env["state"].media_dir, cid)
    for d in dirs:
        d.mkdir(parents=True)
        (d / "slide-01.png").write_bytes(b"x")
    return dirs


def test_discard_removes_media_of_that_content_only(env) -> None:  # noqa: F811
    cid, other = _copy(env), _copy(env)
    dirs, keep = _fill(env, cid), _fill(env, other)
    assert _post(env, f"/review/{cid}/discard").status_code == 303
    assert env["repo"].get_content(cid).status == "discarded"
    assert not any(d.exists() for d in dirs)
    assert all((d / "slide-01.png").is_file() for d in keep)


def test_discard_still_succeeds_when_delete_fails(env, monkeypatch, caplog) -> None:  # noqa: F811
    cid = _copy(env)
    dirs = _fill(env, cid)

    def denied(*a, **kw):
        raise PermissionError("파일이 열려 있음")

    monkeypatch.setattr(M.shutil, "rmtree", denied)
    with caplog.at_level(logging.WARNING):
        r = _post(env, f"/review/{cid}/discard")
    assert r.status_code == 303
    assert env["repo"].get_content(cid).status == "discarded"
    assert all(d.is_dir() for d in dirs)  # 새벽 정리가 다시 시도
    assert any("이미지 정리 실패" in m for m in caplog.messages)


def test_failed_transition_keeps_media(env) -> None:  # noqa: F811
    cid = _copy(env, status="published")
    dirs = _fill(env, cid)
    assert _post(env, f"/review/{cid}/discard").status_code == 409
    assert all(d.is_dir() for d in dirs)


def test_discard_keeps_cards_if_publish_started_meanwhile(env, monkeypatch) -> None:  # noqa: F811
    """폐기 전 검사와 전이 사이에 게시 잡이 그 원고를 올리기 시작했으면 카드를 지우지 않는다."""
    cid = _copy(env, status="approved")
    dirs = _fill(env, cid)
    repo = env["repo"]
    real = repo.publications_for_content
    calls = []

    def pubs(content_id):
        calls.append(content_id)
        rows = real(content_id)
        if len(calls) == 1:
            return rows
        return [p.model_copy(update={"status": "publishing"}) for p in rows[:1]] or rows

    repo.create_publications(cid, ["threads"])
    monkeypatch.setattr(repo, "publications_for_content", pubs)
    assert _post(env, f"/review/{cid}/discard").status_code == 303
    assert repo.get_content(cid).status == "discarded"
    assert all(d.is_dir() for d in dirs)  # 새벽 정리가 지운다


def test_preview_after_discard_does_not_recreate_images(env) -> None:  # noqa: F811
    class Renderer:
        last_overflow: list = []

        def render(self, deck, out):
            out.mkdir(parents=True, exist_ok=True)
            (out / "slide-01.png").write_bytes(b"x")
            return [out / "slide-01.png"]

    env["state"].renderer = Renderer()
    cid = _copy(env)
    assert _post(env, f"/review/{cid}/discard").status_code == 303
    _post(env, f"/review/{cid}/preview")
    assert not (env["state"].media_dir / "previews" / cid).exists()
