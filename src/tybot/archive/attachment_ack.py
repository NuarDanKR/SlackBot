"""첨부가 지금 어떤 결말인가. **사람이 할 일이 다르면 다른 결말이다.**

결정: 2026-10-02 오너 지시(첨부 ACK 는 pending·부분 변환·미지원·차단·원본 저장
실패를 구분하고, 변환 완료 후 갱신한다).

## 왜 갈라야 하나

전에는 전부 `partial` 하나로 들어갔다. 사유는 `error_code` 로 표에 적혔지만
`ingest_ack.read()` 가 그 칸을 안 가져와서 문장까지 오지 못했고, 결국 다섯 가지가
글자 하나까지 같은 말로 나갔다 —

> 원문은 기록했습니다. 첨부 변환 0/1 — 아직 검색에는 안 잡힙니다

이 문장이 맞는 경우는 다섯 중 하나뿐이다. 나머지 넷에서 「아직」 은 거짓말이다.
특히 **원본 저장 실패**에서 제일 나쁘다 — 사람은 기다리면 된다고 읽고, 기다리는
동안 Slack 원본을 지운다. 그러면 되살릴 자료가 어디에도 없다.

## `ready` 로 올리지 않는 것

변환할 수 없는 형식은 **끝났지만 검색되지 않는다.** 끝났다는 이유로 `ready` 로
올리면 그 상태의 뜻이 「전부 검색된다」 에서 「더 할 일이 없다」 로 조용히
바뀌고, `is_searchable()` 을 믿는 모든 경로가 함께 틀린다. 그래서 끝난 것은
`partial` 에 두고 **문장으로** 끝났다고 말한다.

## 여기는 순수 함수다

Slack·DB·파일을 안 본다. 받은 것만 보고 판정한다 — 그래야 수집 경로와 변환
완료 뒤 갱신 경로가 **같은 규칙**을 쓴다. 규칙이 두 벌이면 한쪽만 고치는 날이
오고, 그날부터 들어온 길에 따라 다른 말이 나간다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .archiving_state import (
    ATTACHMENT_ORIGINAL_MISSING_CODE,
    ATTACHMENT_PARTIAL_CODE,
    ATTACHMENT_PENDING_CODE,
    ATTACHMENT_SCREENED_CODE,
    ATTACHMENT_UNSUPPORTED_CODE,
)
from .attachment_doc import BLOCKED, CONVERTED, FAILED, PARTIAL, UNSUPPORTED

log = logging.getLogger("tybot.archive.attachment_ack")

#: 변환이 실패한 것. 미지원(처음부터 못 읽는 형식)과 **다르다** — 이쪽은 읽을 수
#: 있어야 하는데 못 읽은 것이라 사람이 볼 이유가 있다.
ATTACHMENT_CONVERSION_FAILED_CODE = "attachment-conversion-failed"

#: 한 메시지에 결말이 섞였을 때 **무엇을 말할지.** 앞이 이긴다.
#:
#: 순서의 기준은 심각도가 아니라 **사람이 지금 할 일**이다.
#: 1. 원본 저장 실패 — 지금 다시 올려야 한다. 늦으면 되살릴 수 없다
#: 2. 아직 변환 중 — 아직 결말이 아니다. 끝난 것부터 말하면 사람은 다 끝난 줄 안다
#: 3. 변환 실패 — 볼 이유가 있다
#: 4. 수집 규칙 차단 · 5. 미지원 형식 — 끝났고 할 일이 없다
#: 6. 부분 변환 — 그만큼은 쓸 수 있다
PRECEDENCE: tuple[str, ...] = (
    ATTACHMENT_ORIGINAL_MISSING_CODE,
    ATTACHMENT_PENDING_CODE,
    ATTACHMENT_CONVERSION_FAILED_CODE,
    ATTACHMENT_SCREENED_CODE,
    ATTACHMENT_UNSUPPORTED_CODE,
    ATTACHMENT_PARTIAL_CODE,
)

#: 변환 상태 하나를 결말 코드로. 모르는 상태는 **아직**으로 둔다 — 모르는 것을
#: 끝났다고 하면 그 메시지는 다시 안 쳐다본다.
_BY_CONVERSION: dict[str, str] = {
    UNSUPPORTED: ATTACHMENT_UNSUPPORTED_CODE,
    BLOCKED: ATTACHMENT_SCREENED_CODE,
    FAILED: ATTACHMENT_CONVERSION_FAILED_CODE,
    PARTIAL: ATTACHMENT_PARTIAL_CODE,
}


def retained_from_metadata(metadata_path) -> bool | None:
    """staging metadata 가 말하는 **원본 보관 여부.** 모르면 `None`.

    원본은 내려받자마자 쓰고, 그 쓰기의 성패를 같은 자리에서 `original_state` 로
    적는다(`files.stage_attachments`). 그러니 이 값이 보관 여부의 **유일한 1차
    근거**다.

    `attachment_trace.ARCHIVE_DONE` 과 **다른 것을 본다.** 그쪽은 「첨부 참조
    줄이 원문에 들어갔나」 이고, 들어갔다고 원본 바이트가 남은 것은 아니다.
    digest 는 쓰기 **전에** 계산되므로 원본 쓰기가 실패해도 정본은 나온다.
    둘을 같은 것으로 보면 없는 원본을 있다고 세고, 그 메시지가 `ready` 가 된다.
    """
    if metadata_path is None:
        return None
    try:
        meta = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        log.warning("staging metadata 를 읽지 못했다: %s", metadata_path)
        return None
    state = str(meta.get("original_state") or "")
    if state in ("retained", "missing"):
        return state == "retained"
    return None


@dataclass(frozen=True)
class Outcome:
    """첨부 전체의 결말. **문장을 만드는 데 필요한 것만** 든다."""

    total: int
    ready: int
    error_code: str

    @property
    def all_ready(self) -> bool:
        """전부 **검색된다**고 말해도 되는가. 끝났다는 뜻이 아니다."""
        return self.total == 0 or (self.ready == self.total and not self.error_code)


def classify(
    file_ids,
    *,
    stored: dict[str, bool],
    conversion: dict[str, str],
    has_text: dict[str, bool] | None = None,
) -> Outcome:
    """첨부들의 결말 하나.

    - `stored[file_id]` — 원본 바이트를 보관했나. **없으면 `False` 로 읽는다**
      (모르는 것을 보관했다고 하지 않는다)
    - `conversion[file_id]` — 정본 문서의 `conversion_state`. 없으면 아직이다
    - `has_text[file_id]` — 정본에 본문이 있나. 상태가 성공이어도 본문이 비어
      있으면 검색되지 않는다. 안 주면 성공 상태는 본문이 있다고 본다
    """
    ids = [str(fid) for fid in file_ids]
    if not ids:
        return Outcome(0, 0, "")

    text = has_text or {}
    ready = 0
    seen: set[str] = set()
    for fid in ids:
        if not stored.get(fid, False):
            # 원본이 없으면 변환 상태는 볼 필요가 없다. 변환본이 있어도 **정본의
            # 근거가 되는 원본**이 없으므로 나중에 다시 만들지 못한다.
            seen.add(ATTACHMENT_ORIGINAL_MISSING_CODE)
            continue
        state = str(conversion.get(fid) or "")
        if state == CONVERTED and text.get(fid, True):
            ready += 1
            continue
        if state == CONVERTED:
            # 성공이라는데 본문이 없다. 검색되지 않으므로 성공으로 세지 않는다.
            seen.add(ATTACHMENT_CONVERSION_FAILED_CODE)
            continue
        seen.add(_BY_CONVERSION.get(state, ATTACHMENT_PENDING_CODE))

    if not seen:
        return Outcome(len(ids), ready, "")
    for code in PRECEDENCE:
        if code in seen:
            return Outcome(len(ids), ready, code)
    return Outcome(len(ids), ready, ATTACHMENT_PENDING_CODE)  # pragma: no cover
