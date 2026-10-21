"""올린 이미지 읽기(C16, ADR 0045): Claude에 보내는 형식, 비용 기록, 캐시, 정보 없음·실패 처리."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nsm.adapters.sources.image_reader import ClaudeImageReader, make_image_reader, media_type
from nsm.adapters.sources.upload import UploadSource
from nsm.ports import AdapterError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


class FakeMessages:
    def __init__(self, payload: dict, stop_reason: str = "end_turn") -> None:
        self.payload = payload
        self.stop_reason = stop_reason
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=1600, output_tokens=200),
        )


class Sink:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def record_cost(self, kind, provider, usd, ref="") -> None:
        self.rows.append((kind, provider, usd, ref))


def _reader(tmp_path: Path, payload: dict, **kw):
    msgs = FakeMessages(payload, **kw)
    sink = Sink()
    r = ClaudeImageReader(SimpleNamespace(messages=msgs), "claude-haiku-5-5", sink, tmp_path / "c")
    return r, msgs, sink


def test_reads_image_and_records_cost(tmp_path: Path) -> None:
    img = tmp_path / "팝업 공지.png"
    img.write_bytes(PNG)
    r, msgs, sink = _reader(tmp_path, {"has_info": True, "text": "치이카와 팝업 10/20~11/3 성수동"})
    assert r(img) == "치이카와 팝업 10/20~11/3 성수동"
    block = msgs.calls[0]["messages"][0]["content"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == PNG
    assert "팝업 공지" in msgs.calls[0]["messages"][0]["content"][1]["text"]
    assert msgs.calls[0]["model"] == "claude-haiku-5-5"
    assert sink.rows and sink.rows[0][0] == "llm" and sink.rows[0][3] == "read_image"
    assert sink.rows[0][2] > 0


def test_same_file_is_read_once(tmp_path: Path) -> None:
    img = tmp_path / "a.jpg"
    img.write_bytes(JPEG)
    r, msgs, _ = _reader(tmp_path, {"has_info": True, "text": "가격 12,000원"})
    assert r(img) == r(img) == "가격 12,000원"
    assert len(msgs.calls) == 1
    assert msgs.calls[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"


def test_edited_file_is_read_again(tmp_path: Path) -> None:
    import os

    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    r, msgs, _ = _reader(tmp_path, {"has_info": True, "text": "x"})
    r(img)
    img.write_bytes(PNG + b"\x01")
    os.utime(img, (img.stat().st_atime, img.stat().st_mtime + 5))
    r(img)
    assert len(msgs.calls) == 2


def test_no_info_returns_empty(tmp_path: Path) -> None:
    img = tmp_path / "인형.png"
    img.write_bytes(PNG)
    r, _, _ = _reader(tmp_path, {"has_info": False, "text": "하얀 인형 사진"})
    assert r(img) == ""


@pytest.mark.parametrize(
    ("data", "msg"),
    [(b"not an image at all", "PNG·JPEG·WebP"), (PNG + b"\x00" * (5 * 1024 * 1024), "5MB")],
    ids=["not-image", "too-big"],
)
def test_rejects_bad_files_without_calling(tmp_path: Path, data: bytes, msg: str) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(data)
    r, msgs, _ = _reader(tmp_path, {"has_info": True, "text": "x"})
    with pytest.raises(AdapterError, match=msg):
        r(img)
    assert msgs.calls == []


def test_refusal_is_remembered_and_not_resent(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(PNG)
    r, msgs, _ = _reader(tmp_path, {"has_info": True, "text": "x"}, stop_reason="refusal")
    for _ in range(3):  # 자료 모으기가 여러 번 돌아도
        with pytest.raises(AdapterError, match="refusal"):
            r(img)
    assert len(msgs.calls) == 1  # 한 번만 보낸다(다시 돈을 쓰지 않음)
    assert not list((tmp_path / "c").glob("*.txt"))


class Broken:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        raise ConnectionError("network down")


def test_sdk_error_is_friendly_and_retried_next_time(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(PNG)
    msgs = Broken()
    r = ClaudeImageReader(SimpleNamespace(messages=msgs), "claude-haiku-5-5", None, tmp_path / "c")
    for _ in range(2):
        with pytest.raises(AdapterError, match="API 키와 인터넷 연결"):
            r(img)
    assert msgs.calls == 2  # 키·네트워크 문제는 고치면 될 수 있어 남기지 않는다


def test_cache_dir_inside_upload_folder_is_not_material(tmp_path: Path, clock) -> None:
    img = tmp_path / "공지.png"
    img.write_bytes(PNG)
    r, msgs, _ = _reader(tmp_path, {"has_info": True, "text": "팝업 10/20"})
    r.cache_dir = tmp_path / "upload-text"  # 자료 폴더를 NSM_HOME으로 잡은 경우
    items = UploadSource(tmp_path, clock, read_image=r).collect([], 10)
    assert [i.title for i in items] == ["공지"]
    assert len(UploadSource(tmp_path, clock, read_image=r).collect([], 10)) == 1
    assert len(msgs.calls) == 1


def test_media_type_sniffing() -> None:
    assert media_type(PNG) == "image/png"
    assert media_type(JPEG) == "image/jpeg"
    assert media_type(WEBP) == "image/webp"
    assert media_type(b"GIF89a") is None


def test_no_key_means_no_reader(tmp_path: Path, app_config) -> None:
    secrets = SimpleNamespace(get=lambda _k: None)
    assert make_image_reader(app_config, secrets, Sink(), tmp_path) is None


def test_upload_source_reads_images_only_with_reader(tmp_path: Path, clock) -> None:
    (tmp_path / "공지.png").write_bytes(PNG)
    (tmp_path / "사진.jpg").write_bytes(JPEG)
    (tmp_path / "메모.txt").write_text("치이카와 굿즈 메모", encoding="utf-8")
    assert {i.title for i in UploadSource(tmp_path, clock).collect([], 10)} == {"메모"}

    texts = {"공지": "팝업 10/20 성수동", "사진": ""}  # 글자 없는 사진은 자료가 안 된다
    items = UploadSource(tmp_path, clock, read_image=lambda p: texts[p.stem]).collect([], 10)
    by_title = {i.title: i for i in items}
    assert set(by_title) == {"공지", "메모"}
    assert by_title["공지"].text == "팝업 10/20 성수동"
    assert by_title["공지"].source == "upload"


def test_upload_source_skips_broken_image(tmp_path: Path, clock) -> None:
    (tmp_path / "a.png").write_bytes(PNG)

    def boom(_p):
        raise AdapterError("API 오류")

    assert UploadSource(tmp_path, clock, read_image=boom).collect([], 10) == []
