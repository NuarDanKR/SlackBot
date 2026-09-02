"""승인된 첨부 원본을 Claude 에 그대로 보낸다.

검토: [`docs/design/trust-and-usability-review.md`](../../docs/design/trust-and-usability-review.md) §3

## 왜
현장 보고서는 PPT·Excel·스캔 PDF 다. 스캔 PDF 는 텍스트 레이어가 없어 우리 전처리에서
`미변환` 이 되고, 결과적으로 **제목만 아카이브된다.** 원본을 그대로 보내면 모델이
이미지로 읽는다 — 개선이 아니라 없던 기능이 생기는 것이다.

## 형식은 코드가 막는다
Claude 문서 입력은 형식별로 다르다. 안 되는 것을 보내면 400 이 나거나, 더 나쁘게는
사람이 "보냈으니 읽었겠지" 라고 믿는다.

| 형식 | 전송 |
|---|---|
| PDF (스캔 포함) | `document` 블록 |
| 이미지 (png/jpeg/gif/webp) | `image` 블록 |
| 일반 텍스트 | `document` 블록 |
| **xlsx · pptx · docx · hwp** | **보내지 않는다.** 문서 블록 타입이 아니다 |

보내지 못한 형식은 조용히 빠지지 않고 **왜 빠졌는지** 호출자에게 돌려준다.

## 전처리를 대체하지 않는다
원본 전송은 **답변 시점**이고 전처리는 **검색 시점**이다. 전처리가 없으면 "어느 파일을
볼지" 고를 수 없다. 이 모듈은 이미 검색으로 고른 문서의 원본을 덧붙일 뿐이다.

## 출처
`citations` 를 켜면 응답이 인용 단위로 쪼개지고 각 조각에 인용문과 **페이지 번호**가
붙는다. 우리 원칙 2(출처 강제)를 문서 페이지까지 끌어올리는 유일한 방법이다.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tybot.documents")

# 확장자 → (블록 타입, media_type)
PDF_TYPES = {"pdf": "application/pdf"}
IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
TEXT_TYPES = {"txt": "text/plain", "md": "text/plain", "csv": "text/plain"}

# 문서 블록으로 보낼 수 없는 형식. 조용히 빠지면 사람이 읽었다고 믿는다.
UNSUPPORTED_HINT = {
    "xlsx": "Excel 은 원본 전송이 안 됩니다(추출 텍스트로 답합니다)",
    "xlsm": "Excel 은 원본 전송이 안 됩니다(추출 텍스트로 답합니다)",
    "pptx": "PowerPoint 는 원본 전송이 안 됩니다(추출 텍스트로 답합니다)",
    "docx": "Word 는 원본 전송이 안 됩니다(추출 텍스트로 답합니다)",
    "hwp": "한글 문서는 원본 전송이 안 됩니다",
    "hwpx": "한글 문서는 원본 전송이 안 됩니다",
}

# 요청 하나에 담을 수 있는 상한. API 는 32MB 지만 우리는 훨씬 아래에서 끊는다 —
# 큰 파일 하나가 하루 비용과 응답 시간을 통째로 잡아먹는다.
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 3


@dataclass(frozen=True)
class Attached:
    """실제로 붙인 것과 붙이지 못한 이유. 둘 다 사용자에게 보여야 한다."""

    blocks: list[dict]
    included: list[str]
    skipped: list[str]

    @property
    def any(self) -> bool:
        return bool(self.blocks)

    def note(self) -> str:
        """답변에 덧붙일 한 줄. 무엇을 원본으로 읽었는지 밝힌다."""
        parts = []
        if self.included:
            parts.append("원본 첨부로 읽음: " + ", ".join(self.included))
        if self.skipped:
            parts.append("원본으로 읽지 못함 — " + " / ".join(self.skipped))
        return "_" + " · ".join(parts) + "_" if parts else ""


def _ext(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


def block_for(path: Path, name: str, *, citations: bool = True) -> dict | None:
    """파일 하나 → 콘텐츠 블록. 지원하지 않는 형식이면 `None`."""
    ext = _ext(name) or _ext(path.name)
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.warning("첨부 원본을 읽지 못했다 %s: %s", path, e)
        return None
    # base64 는 개행이 들어가면 안 된다.
    data = base64.standard_b64encode(raw).decode("ascii")

    if ext in PDF_TYPES or ext in TEXT_TYPES:
        media = PDF_TYPES.get(ext) or TEXT_TYPES[ext]
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": media, "data": data},
            "title": name[:200],
        }
        if citations:
            # 페이지 단위 출처. 원칙 2 를 문서 안까지 끌고 들어가는 유일한 방법이다.
            block["citations"] = {"enabled": True}
        return block
    if ext in IMAGE_TYPES:
        # 이미지 블록은 citations 를 받지 않는다. 출처는 우리가 붙인 문서 출처로 남는다.
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": IMAGE_TYPES[ext], "data": data},
        }
    return None


def collect(
    items,
    *,
    citations: bool = True,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> Attached:
    """승인된 첨부 목록 → 콘텐츠 블록. 상한과 형식은 여기서 강제한다.

    `items` 는 `attachment_review.Attachment` 처럼 `name`·`object_path`·`size` 를 가진 것.
    """
    blocks: list[dict] = []
    included: list[str] = []
    skipped: list[str] = []
    total = 0

    for item in items or []:
        name = getattr(item, "name", "") or ""
        path = getattr(item, "object_path", None)
        ext = _ext(name)

        if ext in UNSUPPORTED_HINT:
            skipped.append(f"{name}: {UNSUPPORTED_HINT[ext]}")
            continue
        if path is None or not Path(path).is_file():
            skipped.append(f"{name}: 원본 파일을 찾지 못함")
            continue
        if len(blocks) >= max_files:
            skipped.append(f"{name}: 한 번에 {max_files}개까지만 읽습니다")
            continue

        size = Path(path).stat().st_size
        if size > max_file_bytes:
            skipped.append(f"{name}: 너무 큼({size // (1024 * 1024)}MB)")
            continue
        if total + size > max_total_bytes:
            skipped.append(f"{name}: 이번 질문의 첨부 용량 한도를 넘음")
            continue

        block = block_for(Path(path), name, citations=citations)
        if block is None:
            skipped.append(f"{name}: 지원하지 않는 형식")
            continue

        blocks.append(block)
        included.append(name)
        total += size

    if blocks or skipped:
        # 파일명·본문은 로그에 남기지 않는다. 건수와 용량만.
        logger.info(
            "원본 첨부 전송 included=%d skipped=%d bytes=%d",
            len(included), len(skipped), total,
        )
    return Attached(blocks=blocks, included=included, skipped=skipped)


def citation_lines(response_content) -> list[str]:
    """응답에서 인용(페이지 포함)을 뽑아 출처 줄로 만든다.

    모델이 쓴 문장이 아니라 **API 가 붙인 구조화된 인용**이라, 우리가 지어낸 출처가
    아니라는 점이 중요하다.
    """
    out: list[str] = []
    for block in response_content or []:
        cites = getattr(block, "citations", None) or (
            block.get("citations") if isinstance(block, dict) else None
        )
        for c in cites or []:
            get = c.get if isinstance(c, dict) else (lambda k, d=None, _c=c: getattr(_c, k, d))
            title = str(get("document_title", "") or "문서")
            start = get("start_page_number", None)
            end = get("end_page_number", None)
            if start:
                page = f"{start}" if not end or end == start else f"{start}-{end}"
                out.append(f"📄{title} {page}p")
            else:
                out.append(f"📄{title}")
    return list(dict.fromkeys(out))
