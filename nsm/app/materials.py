"""자료 화면(M18): 파일 올리기 검사·저장, 올린 파일 목록, 자료 판정을 사람이 읽을 말로.

라우트는 server.py, 여기는 파일·판정 처리처럼 테스트하기 쉬운 부분이다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nsm.adapters.sources.image_reader import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, media_type
from nsm.adapters.sources.upload import SUPPORTED, ImageReader, NoImageReader, extract_text

MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class MaterialError(ValueError):
    """올린 파일을 쓸 수 없음. 화면에 그대로 보여 준다."""


def safe_name(name: str) -> str:
    """사용자 파일 이름에서 경로·금지 문자를 뺀다(폴더 밖으로 못 나가게)."""
    base = Path(name.replace("\\", "/")).name
    base = _BAD_CHARS.sub("_", base).strip(" .")
    return base[:120] or "자료"


def save_material(
    folder: Path, name: str, data: bytes, read_image: ImageReader | None = None
) -> tuple[Path, int]:
    """파일을 검사해 저장하고 (경로, 읽은 글자 수)를 돌려준다. 읽을 수 없으면 저장하지 않는다.

    이미지는 이미지 읽기(Claude)로 정보를 옮겨 본다. 결과는 캐시에 남아 자료 모으기가 다시 쓴다(C16).
    """
    clean = safe_name(name)
    suffix = Path(clean).suffix.lower()
    if suffix not in SUPPORTED:
        raise MaterialError(
            f"읽을 수 있는 형식이 아닙니다({suffix or '확장자 없음'}). "
            "txt·md·csv·xlsx·pdf와 이미지(png·jpg·webp)만 올릴 수 있습니다"
            "(워드·한글 문서는 PDF로 저장해서 올려 주세요)"
        )
    image = suffix in IMAGE_SUFFIXES
    if image:
        if read_image is None:
            raise MaterialError("이미지를 읽으려면 Claude API 키가 필요합니다(`nsm init`)")
        if len(data) > MAX_IMAGE_BYTES:
            raise MaterialError("이미지는 5MB 이하만 올릴 수 있습니다(화면 캡처는 보통 1~3MB)")
        if media_type(data[:16]) is None:
            raise MaterialError(
                "PNG·JPEG·WebP 이미지가 아닙니다(확장자만 바꾼 파일은 읽지 못합니다)"
            )
    if len(data) > MAX_FILE_BYTES:
        raise MaterialError("20MB 이하 파일만 올릴 수 있습니다")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / clean
    n = 2
    while path.exists():  # 같은 이름이 있으면 덮어쓰지 않고 번호를 붙인다
        path = folder / f"{Path(clean).stem} ({n}){suffix}"
        n += 1
    path.write_bytes(data)
    try:
        text = extract_text(path, read_image)
    except NoImageReader as e:
        path.unlink(missing_ok=True)
        raise MaterialError(str(e)) from e
    except Exception as e:  # noqa: BLE001  깨진 파일·암호 걸린 PDF 등
        path.unlink(missing_ok=True)
        raise MaterialError(f"파일을 읽지 못해 올리지 않았습니다: {type(e).__name__}") from e
    chars = len(text.strip())
    if not chars:
        path.unlink(missing_ok=True)
        if image:
            raise MaterialError(
                "이미지에서 읽을 정보(글자·날짜·가격 등)를 찾지 못해 올리지 않았습니다"
                "(글자가 없는 사진은 자료로 쓸 수 없습니다)"
            )
        raise MaterialError(
            "글자를 하나도 읽지 못해 올리지 않았습니다(스캔한 이미지 PDF는 글자를 읽을 수 없습니다)"
        )
    return path, chars


def list_files(folder: Path, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """올린 파일 목록과 각 파일이 자료로 들어갔는지(같은 파일 주소의 가장 최근 자료)."""
    if not folder.is_dir():
        return []
    by_url: dict[str, dict[str, Any]] = {}
    for it in items:  # items는 최신순이라 처음 본 것이 가장 최근
        if it["url"]:
            by_url.setdefault(it["url"], it)
    out = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        st = path.stat()
        item = by_url.get(path.resolve().as_uri())
        out.append(
            {
                "name": str(path.relative_to(folder)),
                "size_kb": max(1, st.st_size // 1024),
                "modified": datetime.fromtimestamp(st.st_mtime, UTC),
                "supported": path.suffix.lower() in SUPPORTED,
                "item": item,
                # 자료로 들어간 뒤 파일을 고쳤으면 다음 자료 모으기 때 다시 읽는다
                "changed": bool(item and item["collected_at"].timestamp() < st.st_mtime - 1),
            }
        )
    return out


def delete_file(folder: Path, name: str) -> None:
    """uploads 폴더 안의 파일만 지운다(이미 모은 자료는 그대로)."""
    root = folder.resolve()
    path = (folder / name).resolve()
    if root not in path.parents or not path.is_file():
        raise MaterialError("지울 수 없는 파일입니다")
    path.unlink()


def verdict(item: dict[str, Any], threshold: float) -> tuple[str, str]:
    """자료 판정을 (표시 문구, 색) 으로."""
    classifier = item.get("classifier") or ""
    if classifier.startswith("rule:"):
        return f'후기·일기 제외("{classifier[5:]}")', "gray"
    if item.get("label") == "error":
        return "판정 실패", "bad"
    rel = item.get("relevance")
    if rel is None:
        return "판정 전", "gray"
    if rel >= threshold:
        return f"관련 있음 {rel:.2f}", "ok"
    return f"관련 없음 {rel:.2f}", "gray"


SOURCE_LABELS = {
    "naver": "네이버",
    "upload": "올린 파일",
    "google_trends": "구글 트렌드",
    "community": "커뮤니티",
}


def source_label(source: str) -> str:
    if source.startswith("community:"):
        return f"커뮤니티({source.split(':', 1)[1]})"
    return SOURCE_LABELS.get(source, source)
