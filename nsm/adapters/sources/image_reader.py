"""올린 이미지(공지 캡처·포스터·가격표 사진)에서 정보를 글로 옮긴다 (배치 C16, ADR 0045).

Claude가 이미지를 보고 보이는 글자와 사실(상품명·날짜·장소·가격)만 한국어로 옮긴다. 옮긴 글은
다른 올린 파일과 똑같이 자료가 된다. 카드에는 이미지를 넣지 않는다(캐릭터 저작권, ADR 0036).

같은 파일(경로·크기·수정 시각)은 한 번만 읽는다: 대시보드에서 올릴 때 읽은 결과를 캐시에 두고,
자료 모으기가 그 캐시를 다시 쓴다.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from nsm.adapters.pricing import claude_cost
from nsm.ports import AdapterError, CostSink

CACHE_DIR_NAME = "upload-text"  # 자료 폴더를 NSM_HOME으로 잡아도 캐시를 자료로 읽지 않게
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
# Claude API는 이미지 한 장에 5MB까지 받는다
MAX_IMAGE_BYTES = 5 * 1024 * 1024

SYSTEM = (
    "You transcribe information from images for a Korean social media marketing tool. "
    "Write in Korean. Copy the visible text and concrete facts only: product names, dates, "
    "times, places, prices, quantities, event names, conditions, where to buy and notices. "
    "You may state how many product designs or variants are shown (for example '3 designs'). "
    "Write it as short factual lines, e.g. 'CGV 먼작귀 팝콘통 예약판매 오픈', "
    "'구매처: 네이버에서 [CGV 스토어] 검색'. "
    "Do not guess or add anything that is not visible. Do not describe the drawing style "
    "or appearance of characters. If the image has no readable information "
    "(for example a plain photo of an object with no text), set has_info to false."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "has_info": {"type": "boolean"},
        "text": {"type": "string"},
    },
    "required": ["has_info", "text"],
    "additionalProperties": False,
}


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def media_type(data: bytes) -> str | None:
    """파일 앞부분으로 형식을 본다(확장자만 바꾼 파일을 걸러 낸다)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def file_key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve().as_uri()}#{st.st_size}-{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class ClaudeImageReader:
    """`reader(path) -> str`. 읽을 정보가 없으면 빈 문자열."""

    def __init__(
        self, client: Any, model: str, cost_sink: CostSink | None, cache_dir: Path
    ) -> None:
        self.client = client
        self.model = model
        self.cost_sink = cost_sink
        self.cache_dir = cache_dir

    def __call__(self, path: Path) -> str:
        key = file_key(path)
        cache = self.cache_dir / f"{key}.txt"
        if cache.is_file():
            return cache.read_text(encoding="utf-8")
        failed = self.cache_dir / f"{key}.err"
        if failed.is_file():
            # 한 번 못 읽은 파일은 자료 모으기 때마다 다시 보내지 않는다(고치면 키가 바뀌어 다시 읽음)
            raise AdapterError(failed.read_text(encoding="utf-8"))
        data = path.read_bytes()
        if len(data) > MAX_IMAGE_BYTES:
            raise AdapterError("이미지는 5MB 이하만 읽을 수 있습니다")
        mtype = media_type(data)
        if mtype is None:
            raise AdapterError("PNG·JPEG·WebP 이미지가 아닙니다")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            text = self._read(data, mtype, path.stem)
        except AdapterError as e:
            failed.write_text(str(e), encoding="utf-8")
            raise
        except Exception as e:  # noqa: BLE001  키 오류·연결 끊김·요청 제한 등 SDK 예외
            # 키·네트워크 문제는 고치면 다시 될 수 있으므로 실패로 남기지 않는다
            raise AdapterError(
                f"Claude 호출 실패({type(e).__name__}). API 키와 인터넷 연결을 확인하세요"
            ) from e
        cache.write_text(text, encoding="utf-8")
        return text

    def _read(self, data: bytes, mtype: str, title: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM,
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mtype,
                                "data": base64.b64encode(data).decode("ascii"),
                            },
                        },
                        {
                            "type": "text",
                            "text": f"파일 이름: {title}\n이 이미지의 정보를 글로 옮겨 주세요.",
                        },
                    ],
                }
            ],
        )
        usage = getattr(response, "usage", None)
        if self.cost_sink and usage is not None:
            self.cost_sink.record_cost(
                "llm",  # 비용 종류는 동결 계약(CostKind)이라 LLM으로 센다
                "anthropic",
                claude_cost(self.model, usage.input_tokens, usage.output_tokens),
                "read_image",
            )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise AdapterError(f"이미지 응답을 쓸 수 없습니다(stop_reason={response.stop_reason})")
        raw = next((b.text for b in response.content if b.type == "text"), None)
        if raw is None:
            raise AdapterError("이미지 응답에 텍스트가 없습니다")
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AdapterError(f"이미지 응답 JSON 파싱 실패: {e}") from e
        text = str(out.get("text") or "").strip()
        return text if out.get("has_info") and text else ""


def make_image_reader(cfg: Any, secrets: Any, cost_sink: CostSink, cache_dir: Path):
    """Claude 키가 있으면 이미지 읽기, 없으면 None(이미지는 올릴 수 없다고 안내)."""
    key = secrets.get("anthropic_api_key")
    if not key:
        return None
    import anthropic

    return ClaudeImageReader(
        # 사람이 올리고 기다리는 경로라 오래 붙잡지 않는다(SDK 기본은 10분)
        anthropic.Anthropic(max_retries=2, timeout=60.0, api_key=key),
        cfg.settings.models.classifier,
        cost_sink,
        cache_dir,
    )
