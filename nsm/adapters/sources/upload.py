"""직접 올린 자료(가장 믿을 만한 1차 자료). 폴더의 파일을 읽어 자료 항목으로 만든다.

파일 경로 + 크기 + 수정 시각으로 해시를 만들어, 파일을 고치면 새 자료로 다시 들어간다.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from nsm.adapters.sources.common import clip, url_hash
from nsm.domain.clock import new_id
from nsm.domain.models import SourceItem
from nsm.ports import Clock

log = logging.getLogger(__name__)

SUPPORTED = {".txt", ".md", ".csv", ".xlsx", ".pdf"}


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
            return "\n".join(", ".join(row) for row in csv.reader(f))
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    lines.append(", ".join(cells))
        wb.close()
        return "\n".join(lines)
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    raise ValueError(f"지원하지 않는 파일 형식: {path.name}")


class UploadSource:
    name = "upload"

    def __init__(self, folder: Path, clock: Clock) -> None:
        self.folder = folder
        self.clock = clock

    def collect(self, topics: list[str], limit: int) -> list[SourceItem]:
        if not self.folder.is_dir():
            return []
        items: list[SourceItem] = []
        for path in sorted(self.folder.rglob("*")):
            if len(items) >= limit:
                break
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            try:
                text = clip(extract_text(path))
            except Exception as e:  # noqa: BLE001  파일 하나가 깨져도 나머지는 계속
                log.warning("자료 파일을 읽지 못했습니다 %s: %s", path, e)
                continue
            if not text.strip():
                continue
            st = path.stat()
            uri = path.resolve().as_uri()
            items.append(
                SourceItem(
                    id=new_id(),
                    source=self.name,
                    url=uri,
                    title=path.stem,
                    text=text,
                    collected_at=self.clock.now(),
                    url_hash=url_hash(f"{uri}#{st.st_size}-{int(st.st_mtime)}"),
                )
            )
        return items
