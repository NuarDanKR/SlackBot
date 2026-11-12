"""사진 속 글자의 **AI 텍스트 인식(OCR 전사)** — 설명과 다른 파생 revision (B-81 후속).

## 설명과 왜 나누나

설명(`image_description`)은 「무엇이 찍혔나」 를 찾는 데 쓰는 짧은 범주 문장이다.
전사는 「사진에 무슨 글자가 있나」 다. 둘을 한 칸에 섞으면 세 가지가 함께 깨진다.

1. **안전 기준이 다르다.** 설명에는 숫자·식별자가 하나도 들어가면 안 되고(색인이
   PII 통로가 된다), 전사에는 숫자가 들어가는 것이 정상이다 — 대신 마스킹을 거친다.
   한 칸이면 설명의 기준으로 전사가 다 버려지거나, 전사의 기준으로 설명이 느슨해진다.
2. **출처가 다르다.** 전사는 원본 글자를 옮긴 것이라 로컬 OCR 과 같은 축이고,
   설명은 모델의 관찰이다. 어느 쪽에서 나온 말인지 섞이면 되돌릴 수 없다.
3. **불확실성이 다르다.** 「흐려서 글자를 못 읽었다」 와 「사람이 보인다」 는 다른 일이다.

그래서 전사는 **별도 표**(`archiver_image_transcript_revision`)에 **별도 라벨**
`[AI 텍스트 인식·검색용]` 으로 남는다. 설명 표에는 한 글자도 들어가지 않는다.

## 로컬 OCR 이 먼저다

수집 시 로컬 OCR(kordoc)이 늘 먼저 돈다(`convert.py` 의 png/jpg/webp 경로). 이 전사는
그 결과를 **바꾸지 않는다.** 모델이 실패·timeout·무료 한도 소진이면 전사 revision 이
안 생길 뿐이고, 검색은 원래대로 로컬 OCR 줄을 본다.

## 저장 전 검사

1. 형식 — 줄 수·줄 길이·전체 길이 상한. **넘으면 자르지 않고 버린다**(잘린 전사는
   반쪽 문장이 색인에 남는다)
2. 마스킹 — 줄마다 `sensitive_mask` (원칙 5: 가린 뒤 보관)
3. 최종 관문 — 마스킹 뒤 `pii_screen.screen_document` 가 막으면 저장하지 않는다
   (등록부·명단처럼 **문서 자체가 민감물**인 경우)
4. 설명 누출 — 전사 글자가 설명 문장에 섞였으면 설명을 보류한다
   (`description_quotes_transcript`). 판정은 `image_description_output_guard` 가 한다
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LABEL = "[AI 텍스트 인식·검색용]"

STATUS_ABSENT = "absent"
STATUS_TRANSCRIBED = "transcribed"
STATUS_UNCERTAIN = "uncertain"
STATUS_FAILED = "failed"

#: 줄·길이 상한. 프롬프트에는 더 작게 적어 여유를 둔다.
MAX_LINES = 200
MAX_LINE_CHARS = 300
MAX_TOTAL_CHARS = 8000
PROMPT_MAX_LINES = 150

#: 전사 불확실성 코드. 설명의 코드와 **다른 목록**이다 — 할 일이 다르다.
UNCERTAINTY_CODES = frozenset(
    {
        "partial_text",  # 일부만 읽혔다
        "handwriting",  # 손글씨라 틀렸을 수 있다
        "low_resolution",
        "blurred",
        "skewed",  # 기울거나 곡면이라 줄 순서가 불확실
        "occluded",
        "plate_redacted",  # 차량 번호를 옮기지 않고 가렸다
    }
)
MAX_UNCERTAINTY = 6

ERROR_INVALID = "transcript_invalid"
ERROR_TOO_LONG = "transcript_too_long"
ERROR_EMPTY = "transcript_empty"

CODE_PII_BLOCKED = "transcript_pii_blocked"
CODE_MASKED = "transcript_masked"
CODE_LEAK = "description_quotes_transcript"

#: 설명에 전사가 섞였다고 보는 최소 길이(공백 제외 글자). 짧으면 「안전」 「출입」 같은
#: 일반 낱말이 우연히 겹친다. 8글자 이상이 같은 순서로 나오면 옮겨 적은 것으로 본다.
LEAK_WINDOW = 8


@dataclass(frozen=True)
class ImageTranscript:
    """모델이 돌려준 전사. **고치지 않은 기록**이다 — 저장 판정은 따로 만든다."""

    status: str = STATUS_ABSENT
    lines: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    error_code: str = ""

    @property
    def present(self) -> bool:
        return self.status in (STATUS_TRANSCRIBED, STATUS_UNCERTAIN) and bool(self.lines)


@dataclass(frozen=True)
class TranscriptDecision:
    """저장 판정. `storable` 이면 `body` 는 **마스킹이 끝난** 줄들이다."""

    storable: bool
    status: str = ""
    body: str = ""
    line_count: int = 0
    masked_count: int = 0
    uncertainty: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()


def _clean(value) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def parse_transcript(value) -> ImageTranscript:
    """모델 JSON 의 `transcript` 칸을 읽는다. **예외를 내지 않는다.**

    칸이 없으면 `absent` 다(옛 판 응답). 형식이 틀리면 `failed` 로 닫고 설명은
    따로 판정한다 — 전사가 틀렸다고 설명까지 버릴 이유는 없다.
    """
    if value is None:
        return ImageTranscript()
    if not isinstance(value, dict):
        return ImageTranscript(status=STATUS_FAILED, error_code=ERROR_INVALID)
    present = value.get("present")
    lines = value.get("lines", [])
    uncertainty = value.get("uncertainty", [])
    if (
        type(present) is not bool
        or not isinstance(lines, list)
        or not isinstance(uncertainty, list)
    ):
        return ImageTranscript(status=STATUS_FAILED, error_code=ERROR_INVALID)
    codes: list[str] = []
    for item in uncertainty:
        code = str(item).strip()
        if code in UNCERTAINTY_CODES and code not in codes:
            codes.append(code)
        if len(codes) >= MAX_UNCERTAINTY:
            break
    if not present:
        return ImageTranscript(uncertainty=tuple(codes))
    if any(not isinstance(item, str) for item in lines):
        return ImageTranscript(status=STATUS_FAILED, error_code=ERROR_INVALID)
    cleaned = [_clean(item) for item in lines]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        # 「글자가 있다」 면서 한 줄도 없으면 아무것도 알려 주지 않은 것이다.
        return ImageTranscript(status=STATUS_FAILED, error_code=ERROR_EMPTY)
    if (
        len(cleaned) > MAX_LINES
        or any(len(item) > MAX_LINE_CHARS for item in cleaned)
        or sum(len(item) for item in cleaned) > MAX_TOTAL_CHARS
    ):
        # **자르지 않고 버린다.** 상한을 넘겼다는 것은 규칙을 안 지켰다는 뜻이다.
        return ImageTranscript(status=STATUS_FAILED, error_code=ERROR_TOO_LONG)
    return ImageTranscript(
        status=STATUS_UNCERTAIN if codes else STATUS_TRANSCRIBED,
        lines=tuple(cleaned),
        uncertainty=tuple(codes),
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text or "")).lower()


def leaks_into(description: str, transcript: ImageTranscript | None) -> bool:
    """설명 문장에 전사 글자가 옮겨 적혔나.

    전사의 각 줄에서 공백을 뺀 `LEAK_WINDOW` 글자 조각이 설명(공백 제외)에 그대로
    나오면 옮겨 적은 것으로 본다. 판정만 하고 **어느 조각인지는 돌려주지 않는다**
    (그 조각이 로그로 새면 막으려던 자리 밖으로 나간다).
    """
    if not transcript or not transcript.present or not description:
        return False
    target = _compact(description)
    if len(target) < LEAK_WINDOW:
        return False
    for line in transcript.lines:
        compact = _compact(line)
        for start in range(0, max(0, len(compact) - LEAK_WINDOW) + 1):
            window = compact[start : start + LEAK_WINDOW]
            if len(window) == LEAK_WINDOW and window in target:
                return True
    return False


def guard_transcript(
    transcript: ImageTranscript | None, *, filename: str = ""
) -> TranscriptDecision:
    """전사를 저장해도 되나. **마스킹 → 최종 관문** 순서로 본다.

    마스킹은 줄이 완성된 뒤 한 번 더 보는 구조와 같다(원칙 5). 마스킹이 못 가린 모양이
    남았거나 문서 자체가 민감물이면 저장하지 않는다 — 버린 사실은 코드로만 남긴다.
    """
    if not transcript or not transcript.present:
        return TranscriptDecision(storable=False)
    from ..pii_screen import screen_document, screen_line
    from ..sensitive_mask import mask_sensitive

    masked_lines: list[str] = []
    masked = 0
    for line in transcript.lines:
        result = mask_sensitive(line)
        masked_lines.append(result.text)
        if result.text != line:
            masked += 1
    body = "\n".join(masked_lines)
    screened = screen_document(body, filename=filename)
    if screened.blocked or any(screen_line(line) for line in masked_lines):
        logger.info("image_transcript_guard blocked lines=%d", len(masked_lines))
        return TranscriptDecision(storable=False, codes=(CODE_PII_BLOCKED,))
    codes = (CODE_MASKED,) if masked else ()
    return TranscriptDecision(
        storable=True,
        status=transcript.status,
        body=body,
        line_count=len(masked_lines),
        masked_count=masked,
        uncertainty=transcript.uncertainty,
        codes=codes,
    )


__all__ = [
    "CODE_LEAK",
    "CODE_MASKED",
    "CODE_PII_BLOCKED",
    "ERROR_EMPTY",
    "ERROR_INVALID",
    "ERROR_TOO_LONG",
    "LABEL",
    "LEAK_WINDOW",
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "MAX_TOTAL_CHARS",
    "PROMPT_MAX_LINES",
    "STATUS_ABSENT",
    "STATUS_FAILED",
    "STATUS_TRANSCRIBED",
    "STATUS_UNCERTAIN",
    "UNCERTAINTY_CODES",
    "ImageTranscript",
    "TranscriptDecision",
    "guard_transcript",
    "leaks_into",
    "parse_transcript",
]
