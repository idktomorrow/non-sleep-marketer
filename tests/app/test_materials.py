"""자료 화면(M18): 파일 올리기·읽기 확인·올린 파일 상태·모은 자료 목록."""

from __future__ import annotations

import pytest

from nsm.adapters.sources.upload import UploadSource
from nsm.app import materials as M
from tests.app.test_dashboard import _post, env  # noqa: F401  같은 대시보드 픽스처


@pytest.fixture
def menv(env, tmp_path):  # noqa: F811
    env["state"].uploads_dir = tmp_path / "uploads"
    env["dir"] = tmp_path / "uploads"
    return env


def _upload(env, name: str, data: bytes):  # noqa: F811
    return env["client"].post(
        "/materials/upload",
        data={"csrf": env["state"].csrf_token},
        files={"file": (name, data)},
        follow_redirects=False,
    )


def test_upload_reads_text_and_shows_pending_file(menv) -> None:
    r = _upload(menv, "치이카와 신상 안내.txt", "치이카와 팝업 10월 20일 성수동 오픈".encode())
    assert r.status_code == 303 and "uploaded=" in r.headers["location"]
    assert (menv["dir"] / "치이카와 신상 안내.txt").is_file()
    html = menv["client"].get(r.headers["location"]).text
    assert "올렸습니다" in html and "글자 22자를 읽었습니다" in html
    assert "치이카와 신상 안내.txt" in html and "아직 안 읽음" in html


def test_collected_upload_shows_verdict(menv) -> None:
    _upload(menv, "팝업.md", "치이카와 팝업 정보 정리 문서입니다".encode())
    repo = menv["repo"]
    item = UploadSource(menv["dir"], menv["state"].clock).collect([], 10)[0]
    repo.insert_source_item(item)
    repo.set_source_relevance(item.id, 0.93)
    html = menv["client"].get("/materials").text
    assert "관련 있음 0.93" in html and "올린 파일" in html
    assert "관련 있음 0.93" in menv["client"].get("/materials?source=upload").text


@pytest.mark.parametrize(
    ("name", "data", "msg"),
    [
        ("보고서.docx", b"PK\x03\x04", "읽을 수 있는 형식이 아닙니다"),
        ("빈 파일.txt", b"   \n", "글자를 하나도 읽지 못해"),
        ("깨진.pdf", b"%PDF-1.4 garbage", "파일을 읽지 못해"),
    ],
)
def test_unusable_files_are_not_kept(menv, name: str, data: bytes, msg: str) -> None:
    r = _upload(menv, name, data)
    assert "error=" in r.headers["location"]
    assert msg in menv["client"].get(r.headers["location"]).text
    assert not list(menv["dir"].glob("*")) if menv["dir"].exists() else True


def test_file_names_cannot_escape_and_do_not_overwrite(menv) -> None:
    _upload(menv, "../../밖으로.txt", "치이카와 자료".encode())
    _upload(menv, "밖으로.txt", "두 번째 자료".encode())
    names = sorted(p.name for p in menv["dir"].iterdir())
    assert names == ["밖으로 (2).txt", "밖으로.txt"]
    assert not (menv["dir"].parent / "밖으로.txt").exists()


def test_too_large_file_rejected(menv, monkeypatch) -> None:
    monkeypatch.setattr(M, "MAX_FILE_BYTES", 10)
    with pytest.raises(M.MaterialError, match="20MB"):
        M.save_material(menv["dir"], "큰.txt", b"x" * 11)


def test_delete_only_inside_folder(menv) -> None:
    _upload(menv, "지울 파일.txt", "치이카와".encode())
    assert _post(menv, "/materials/files/delete", name="../test.txt").status_code == 400
    r = _post(menv, "/materials/files/delete", name="지울 파일.txt")
    assert r.status_code == 303 and not (menv["dir"] / "지울 파일.txt").exists()


def test_materials_page_lists_collected_items_with_filter(menv) -> None:
    html = menv["client"].get("/materials").text
    assert "모은 자료" in html and 'href="/materials"' in html  # 메뉴
    assert "판정 전" in html or "관련" in html
    assert menv["client"].get("/materials?source=bogus").status_code == 200


def test_verdict_texts() -> None:
    assert M.verdict({"classifier": "rule:다녀왔어"}, 0.8)[0] == '후기·일기 제외("다녀왔어")'
    assert M.verdict({"label": "error", "classifier": "gemini"}, 0.8)[0] == "판정 실패"
    assert M.verdict({"relevance": None}, 0.8)[0] == "판정 전"
    assert M.verdict({"relevance": 0.5, "classifier": "gemini"}, 0.8) == ("관련 없음 0.50", "gray")
    assert M.source_label("community:cafe.example.com") == "커뮤니티(cafe.example.com)"


# ---- 이미지 자료(C16, ADR 0045)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_image_upload_is_read_and_preview_shown(menv) -> None:
    seen = []

    def read(path):
        seen.append(path.name)
        return "CGV 먼작귀 팝콘통 예약판매 오픈\n구매처: 네이버에서 [CGV 스토어] 검색"

    menv["state"].read_image = read
    r = _upload(menv, "cgv 팝콘통.png", PNG)
    assert "uploaded=" in r.headers["location"] and "preview=" in r.headers["location"]
    html = menv["client"].get(r.headers["location"]).text
    assert "이미지에서 정보" in html and "CGV 먼작귀 팝콘통 예약판매 오픈 구매처" in html
    assert (menv["dir"] / "cgv 팝콘통.png").is_file() and seen
    # 간단 모드 "오늘"에서 올려도 같은 안내
    r = menv["client"].post(
        "/materials/upload",
        data={"csrf": menv["state"].csrf_token, "next": "/"},
        files={"file": ("포스터.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 16)},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/?")
    assert "이미지에서 정보" in menv["client"].get(r.headers["location"]).text


@pytest.mark.parametrize(
    ("reader", "data", "msg"),
    [
        (None, PNG, "API 키가 필요"),
        (lambda p: "", PNG, "읽을 정보(글자·날짜·가격 등)를 찾지 못해"),
        (lambda p: "x", b"plain text", "PNG·JPEG·WebP 이미지가 아닙니다"),
        (lambda p: "x", PNG + b"\x00" * (5 * 1024 * 1024), "5MB 이하"),
    ],
    ids=["no-key", "no-info", "fake-png", "too-big"],
)
def test_unusable_images_are_not_kept(menv, reader, data: bytes, msg: str) -> None:
    menv["state"].read_image = reader
    r = _upload(menv, "사진.png", data)
    assert "error=" in r.headers["location"]
    assert msg in menv["client"].get(r.headers["location"]).text
    assert not (menv["dir"] / "사진.png").exists()


def test_upload_form_accepts_images(menv) -> None:
    assert ".png,.jpg,.jpeg,.webp" in menv["client"].get("/materials").text


def test_upload_then_collect_calls_claude_once(menv, tmp_path) -> None:
    """올릴 때 읽은 결과를 자료 모으기가 다시 쓴다(이 기능의 핵심 약속)."""
    import json
    from types import SimpleNamespace

    from nsm.adapters.sources.image_reader import ClaudeImageReader

    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text", text=json.dumps({"has_info": True, "text": "팝콘통 예약판매"})
                )
            ],
            usage=SimpleNamespace(input_tokens=1600, output_tokens=20),
        )

    reader = ClaudeImageReader(
        SimpleNamespace(messages=SimpleNamespace(create=create)),
        "claude-haiku-5-5",
        menv["repo"],
        tmp_path / "upload-text",
    )
    menv["state"].read_image = reader
    r = _upload(menv, "팝콘통.png", PNG)
    assert "preview=" in r.headers["location"]
    items = UploadSource(menv["dir"], menv["state"].clock, read_image=reader).collect([], 10)
    assert [i.text for i in items] == ["팝콘통 예약판매"]
    assert len(calls) == 1


def test_reader_failure_message_is_not_broken_file(menv) -> None:
    from nsm.ports import AdapterError

    def boom(_p):
        raise AdapterError(
            "Claude 호출 실패(AuthenticationError). API 키와 인터넷 연결을 확인하세요"
        )

    menv["state"].read_image = boom
    r = _upload(menv, "공지.png", PNG)
    html = menv["client"].get(r.headers["location"]).text
    assert "이미지를 읽지 못해 올리지 않았습니다" in html and "API 키와 인터넷 연결" in html
    assert not (menv["dir"] / "공지.png").exists()


def test_preview_and_name_are_escaped(menv) -> None:
    menv["state"].read_image = lambda p: "<script>alert(1)</script> 팝업"
    r = _upload(menv, "<b>x</b>.png", PNG)
    html = menv["client"].get(r.headers["location"]).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_long_preview_is_cut_with_ellipsis(menv) -> None:
    menv["state"].read_image = lambda p: "가" * 150 + "\n\n\n" + "나" * 150
    r = _upload(menv, "긴 공지.png", PNG)
    html = menv["client"].get(r.headers["location"]).text
    assert "…”" in html
    menv["state"].read_image = lambda p: "짧은 줄\n" * 40  # 줄바꿈이 많아도 자르지 않았으면 … 없음
    r = _upload(menv, "줄 많은 공지.png", PNG)
    html = menv["client"].get(r.headers["location"]).text
    assert "짧은 줄 짧은 줄" in html and "…”" not in html
