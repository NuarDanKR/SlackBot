"""B-100 — 이미지 처리 상태를 원본 스레드의 **댓글 하나**로 보여 준다.

## 무엇을 하나
사람이 이미지를 올리면 그 메시지 스레드에 상태 댓글이 하나 달리고, 처리가 진행될
때마다 **같은 댓글을 고친다**(`chat.update`). 상태마다 새 댓글을 달면 스레드가 상태
로그가 되고, 사람이 실제 대화를 찾지 못한다(2026-11-24 오너 결정).

```
accepted ─▶ processing ─▶ completed | uncertain
   │             └──────▶ deferred(다음 배치 재시도) | failed(자동 재시도 없음)
   └────────────────────▶ deferred | failed
```

## 누가 무엇을 쓰나
- **Archiver** 가 수집 직후 `accepted` 를 적고, 댓글을 게시·수정한다. Slack 토큰은
  Archiver 만 갖는다.
- **Vision worker** 는 `processing`·`completed`·`uncertain`·`deferred`·`failed` 를
  **DB 에만** 적는다. Slack 을 모른다. 적기가 실패해도 Vision 결과는 그대로다 —
  알림 때문에 모델을 다시 부르지 않는다.

## 멱등 — 같은 단계는 한 번만 보인다
행 하나가 이미지 하나다 — `(workspace, channel_id, message_ts, file_id)`. 단계가
**실제로 앞으로 갈 때만** `version` 이 오른다(:func:`advance`). 같은 단계를 다시 적는
것(worker 재시작·같은 job 재실행·Slack 재전달)은 아무것도 바꾸지 않는다. Archiver 는
`version > delivered_version` 인 행만 Slack 에 반영하므로 같은 단계 댓글이 두 번 나가지
않는다.

게시가 성공했는데 `reply_ts` 를 적기 전에 프로세스가 죽으면 다음 시도가 새 댓글을
달 수 있다. 그래서 댓글에 메시지 metadata(:data:`METADATA_EVENT`)로 **이미지 키의
해시**를 붙이고, 앞선 시도가 있었던 행은 게시 전에 스레드에서 그 키를 먼저 찾는다.

## 보이는 것과 보이지 않는 것
- `Vision LLM` 과 `기본 OCR` 을 늘 따로 적는다. Vision 이 끝나기 전에는 「인식 완료」 라고
  쓰지 않는다. `uncertain` 은 「인식 완료 · 확인 필요」 다 — 성공으로 꾸미지 않는다.
- 「검색 반영: 완료」 는 검색 투영(`image_search_entry`)이 확인된 뒤에만 쓴다.
- 인식 결과는 가드를 지난 저장값만, 다시 가린 뒤(`sensitive_mask`) 12줄까지만 싣는다.
  예외 본문·provider 응답·키·URL 은 싣지 않는다. 오류는 고정 코드뿐이다.
- 이 댓글은 봇 메시지라 Archiver 수집에서 빠진다(원칙 1 — `ingest_event` 의 `bot_id` 거름).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from tybot.sensitive_mask import contains_sensitive, mask_sensitive

from .image_description_notification import (
    MAX_TRANSCRIPT_LINES,
    recognition_fields,
)

log = logging.getLogger("tybot.archive.image_status_notice")

_WORKSPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CHANNEL = re.compile(r"^[CG][A-Z0-9]{8,31}$")
_TS = re.compile(r"^\d{1,20}\.\d{1,9}$")
_FILE = re.compile(r"^F[A-Z0-9]{8,31}$")
_JOB = re.compile(r"^[a-f0-9]{32}$")
_CODE = re.compile(r"^[a-z0-9_]{1,64}$")

ACCEPTED = "accepted"
PROCESSING = "processing"
COMPLETED = "completed"
UNCERTAIN = "uncertain"
DEFERRED = "deferred"
FAILED = "failed"
PHASES = (ACCEPTED, PROCESSING, COMPLETED, UNCERTAIN, DEFERRED, FAILED)
#: 같은 job 안에서는 순위가 오를 때만 앞으로 간다. 끝 상태끼리는 넘나들지 않는다.
RANK = {
    ACCEPTED: 10,
    PROCESSING: 20,
    DEFERRED: 30,
    FAILED: 30,
    UNCERTAIN: 40,
    COMPLETED: 40,
}
_SUCCESS = frozenset({COMPLETED, UNCERTAIN})

#: 다음 배치에서 **정말로** 다시 시도되는 사유. 예약 전에 멈춘 후보만 그렇다 —
#: 한 번 provider 에 나간 job 은 중복 과금을 막으려고 같은 원본을 다시 보내지
#: 않는다(`ImageDescriptionStore.reserve` 의 `duplicate_job`).
RETRY_NEXT_BATCH = frozenset(
    {
        "provider_temporarily_unavailable",
        "checks_failed",
        "reserve_failed",
        "approval_lookup_failed",
        "catalog_unavailable",
    }
)
#: 호출·무료 한도. 예약 전에 막혀 다음 배치에서 다시 시도된다.
RETRY_AFTER_LIMIT = frozenset(
    {
        "quota_exhausted",
        "daily_calls_exceeded",
        "concurrency_exceeded",
    }
)
#: provider 일시 오류로 끝난 job. 보류지만 자동 재시도는 없다.
PROVIDER_TRANSIENT = frozenset({"model_timeout", "model_error"})
#: 이미 끝난 job 이 다시 막힌 것 — 새로 알릴 사실이 없다.
SILENT_CODES = frozenset({"duplicate_job", "image_description_disabled"})

#: Vision 이 쓰는 이미지 형식(`image_description_source.FILETYPE_MIME` 과 같다).
VISION_FILETYPES = frozenset({"png", "jpg", "jpeg", "webp"})

METADATA_EVENT = "tybot_image_status"
MAX_FILE_LABEL = 80
MAX_ATTEMPTS = 20
#: 등록 해제·멤버 아님·채널 없음. 다시 보내도 같다 — 콘솔에 실패로 남긴다.
TERMINAL_SLACK_ERRORS = frozenset(
    {
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "thread_not_found",
        "restricted_action",
        # 앱에 chat:write 가 없거나 토큰이 죽었다 — 고치기 전에는 몇 번을 보내도 같다.
        "missing_scope",
        "not_authed",
        "invalid_auth",
        "account_inactive",
    }
)
#: 댓글을 고칠 수 없다 — 이때만 새 댓글을 달고 `reply_ts` 를 바꾼다.
REPOST_SLACK_ERRORS = frozenset(
    {
        "message_not_found",
        "cant_update_message",
        "edit_window_closed",
    }
)


class NoticeError(ValueError):
    """입력 좌표가 계약에 맞지 않는다. 본문 없이 코드만 싣는다."""


@dataclass(frozen=True)
class NoticeKey:
    workspace: str
    channel_id: str
    message_ts: str
    file_id: str

    def __post_init__(self) -> None:
        if not _WORKSPACE.fullmatch(self.workspace):
            raise NoticeError("workspace_invalid")
        if not _CHANNEL.fullmatch(self.channel_id):
            raise NoticeError("channel_invalid")
        if not _TS.fullmatch(self.message_ts):
            raise NoticeError("message_ts_invalid")
        if not _FILE.fullmatch(self.file_id):
            raise NoticeError("file_invalid")

    @property
    def digest(self) -> str:
        """Slack metadata 에 싣는 키. 좌표만으로 만들고 본문은 섞지 않는다."""
        body = f"{self.workspace}|{self.channel_id}|{self.message_ts}|{self.file_id}"
        return hashlib.sha256(body.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class NoticeRow:
    key: NoticeKey
    phase: str
    job_id: str = ""
    job_created_at: datetime | None = None
    payload: dict = field(default_factory=dict)
    version: int = 1
    #: 실시간 접수된 이미지만 참이다 — Slack 댓글 대상. 소급·과거 이미지는 DB·콘솔에만 남는다.
    slack_eligible: bool = False
    delivered_version: int = 0
    reply_ts: str = ""
    delivery_state: str = "pending"
    #: 아래 시도 칸(attempts·next_attempt_at·error_code)이 몇 번째 판의 것인가.
    attempt_version: int = 0
    attempts: int = 0
    last_attempt_at: datetime | None = None
    next_attempt_at: datetime | None = None
    error_code: str = ""


# ---------------------------------------------------------------------------
# 보여 줄 값 — 저장 전에 가리고 자른다
# ---------------------------------------------------------------------------


def _clip(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def file_label(name: str) -> str:
    """사람이 어느 이미지인지 알아볼 이름. 가린 뒤 자르고, 못 가린 모양이면 버린다."""
    label = _clip(mask_sensitive(str(name or "")).text, MAX_FILE_LABEL)
    return "" if not label or contains_sensitive(label) else label


def ocr_state(conversion_state: str, has_text: bool) -> str:
    """기본 OCR(변환기) 결과를 세 값으로 접는다."""
    if conversion_state in {"succeeded", "partial"}:
        return "text" if has_text else "empty"
    if conversion_state == "pending":
        return "pending"
    return "none"


def safe_code(value: object) -> str:
    code = str(value or "")
    return code if _CODE.fullmatch(code) else ""


def receipt_payload(
    *, name: str, conversion_state: str, has_text: bool, vision_enabled: bool
) -> dict:
    return {
        "file": file_label(name),
        "ocr": ocr_state(conversion_state, has_text),
        "vision_enabled": bool(vision_enabled),
    }


# ---------------------------------------------------------------------------
# 렌더링 — 단계마다 고정 문장. 모델 본문은 저장된 가드 통과값만
# ---------------------------------------------------------------------------

_OCR_TEXT = {
    "text": "보관 완료",
    "empty": "보관 완료(읽은 글자 없음)",
    "pending": "변환 대기",
    "none": "사용할 수 없음",
}
_PROVIDER = {"google": "Google", "anthropic": "Anthropic", "openai": "OpenAI"}


def _slack_safe(text: str) -> str:
    """모델·사람 글자가 멘션·링크가 되지 않게 Slack 제어 문자를 이스케이프한다."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _head_lines(payload: dict) -> list[str]:
    lines = []
    if payload.get("file"):
        lines.append(f"- 파일: {payload['file']}")
    return lines


def _ocr_line(payload: dict) -> str:
    return f"- 기본 OCR: {_OCR_TEXT.get(str(payload.get('ocr') or ''), '확인 중')}"


def render(phase: str, payload: dict, *, shown_transcript=None, table_below: bool = False) -> str:
    """한 단계의 댓글 본문. 마지막에 다시 가리고 Slack 제어 문자를 막는다.

    ``shown_transcript`` 는 글로 보일 전사 줄이다(기본: 전부). 표로 옮긴 줄을 빼고 부를 때
    쓰며, 「텍스트 인식: 완료」 판정은 언제나 원래 전사로 한다. ``table_below`` 면 표가
    아래에 따로 붙는다고 적는다.
    """
    if phase not in PHASES:
        raise NoticeError("phase_invalid")
    lines: list[str]
    if phase == ACCEPTED:
        lines = ["✅ 이미지 접수 완료", *_head_lines(payload), _ocr_line(payload)]
        if payload.get("vision_enabled"):
            lines.append("- Vision LLM: 처리 대기")
        else:
            lines.append("- Vision LLM: 사용 안 함 (기본 OCR 자료만 씁니다)")
    elif phase == PROCESSING:
        lines = [
            "⏳ 이미지 인식 처리 중",
            *_head_lines(payload),
            _ocr_line(payload),
            "- Vision LLM: 처리 중",
        ]
    elif phase in _SUCCESS:
        lines = [
            "🟡 이미지 인식 완료 · 확인 필요" if phase == UNCERTAIN else "✅ 이미지 인식 완료",
            *_head_lines(payload),
        ]
        provider = _PROVIDER.get(str(payload.get("provider") or ""), "")
        lines.append(f"- 처리 방식: {provider + ' ' if provider else ''}Vision LLM")
        transcript = tuple(payload.get("transcript") or ())
        lines.append("- 텍스트 인식: " + ("완료" if transcript else "읽은 글자 없음"))
        lines.append("- 이미지 설명: " + ("완료" if payload.get("description") else "보류"))
        lines.append("- 검색 반영: " + ("완료" if payload.get("search_published") else "확인 중"))
        if phase == UNCERTAIN:
            lines.append("- 일부 내용이 불확실합니다. 원본 이미지와 함께 확인해 주세요.")
        if payload.get("description"):
            lines.append(f"*설명* {payload['description']}")
        tags = tuple(payload.get("tags") or ())
        if tags:
            lines.append("*태그* " + ", ".join(tags))
        shown = (
            transcript[:MAX_TRANSCRIPT_LINES]
            if shown_transcript is None
            else tuple(shown_transcript)
        )
        if transcript:
            lines.append("*읽은 글자* (민감정보는 가림)")
            if table_below:
                lines.append("│ (표는 아래에 정리했습니다)")
            lines.extend(f"│ {line}" for line in shown)
            more = int(payload.get("transcript_more") or 0)
            if more > 0:
                lines.append(f"│ …추가 {more}줄은 검색에 반영됨")
        lines.append("_AI 인식 결과입니다. 원본 이미지를 확인해 주세요._")
    elif phase == DEFERRED:
        lines = [
            "⚠️ Vision LLM 처리 보류",
            *_head_lines(payload),
            "- 현재 사용 자료: 기본 OCR",
            (
                "- 상태: 호출 한도에 닿아 다음 배치에서 재시도합니다"
                if payload.get("reason") == "limit"
                else "- 상태: 외부 모델 일시 오류로 다음 배치에서 재시도합니다"
            ),
        ]
    else:  # FAILED
        reason = str(payload.get("reason") or "")
        if reason == "provider_error":
            status = (
                "외부 모델 일시 오류로 처리하지 못했습니다. 중복 과금을 막기 위해 "
                "같은 요청을 자동으로 다시 보내지 않습니다"
            )
        elif reason == "unusable_output":
            status = "인식 결과를 검색에 쓸 수 없었습니다"
        else:
            status = "처리 조건을 통과하지 못했습니다"
        lines = [
            "⚠️ Vision LLM 처리 안 됨",
            *_head_lines(payload),
            "- 현재 사용 자료: 기본 OCR",
            f"- 상태: {status}. 관리 콘솔에서 상태를 확인해 주세요",
        ]
    return _slack_safe(mask_sensitive("\n".join(lines)).text)


# ---------------------------------------------------------------------------
# 블록 — 전사 속 표를 Slack 표 블록으로
# ---------------------------------------------------------------------------

#: Slack 이 블록을 거부하면 표 없는 글 블록으로 한 번 더 보낸다.
INVALID_BLOCK_ERRORS = frozenset({"invalid_blocks", "invalid_blocks_format"})
MAX_TABLE_COLUMNS = 20
#: section 블록 text 상한(3000)보다 여유 있게 나눈다.
_SECTION_CHARS = 2900
_TABLE_RULE = re.compile(r"^[\s|:\-]+$")


def _cells(line: str) -> list[str] | None:
    """`a | b | c` 한 줄 → 칸 목록. 표 행이 아니면 ``None``."""
    text = line.strip()
    if "|" not in text:
        return None
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells = [cell.strip() for cell in text.split("|")]
    return cells if len(cells) >= 2 else None


def table_span(lines) -> tuple[int, int, list[list[str]]] | None:
    """전사 줄에서 **첫 번째** 표 구간(시작, 끝, 행들). 두 줄 이상 이어진 `|` 행만 표다.

    Vision 프롬프트(2026-11-26.1)는 표를 「헤더 한 줄 + 한 행을 한 줄」 로, 칸을 ` | ` 로
    잇는다. 마크다운 구분선(`---|---`)은 행이 아니므로 건너뛴다. 메시지당 표는 하나다.
    """
    items = list(lines)
    index = 0
    while index < len(items):
        if _cells(items[index]) is None or _TABLE_RULE.fullmatch(items[index]):
            index += 1
            continue
        start = index
        rows: list[list[str]] = []
        while index < len(items):
            line = items[index]
            if _TABLE_RULE.fullmatch(line) and "|" in line:
                index += 1
                continue
            cells = _cells(line)
            if cells is None:
                break
            rows.append(cells)
            index += 1
        if len(rows) >= 2:
            width = max(len(row) for row in rows)
            if width > MAX_TABLE_COLUMNS:
                return None
            return start, index, [row + [""] * (width - len(row)) for row in rows]
    return None


def _sections(text: str) -> list[dict]:
    """mrkdwn 글을 section 블록들로. 줄 경계에서 나눈다."""
    blocks: list[dict] = []
    chunk: list[str] = []
    size = 0
    for line in text.split("\n"):
        if chunk and size + len(line) + 1 > _SECTION_CHARS:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
            chunk, size = [], 0
        chunk.append(line[:_SECTION_CHARS])
        size += len(line) + 1
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
    return blocks


def _table_cell(text: str) -> dict:
    # raw_text 는 mrkdwn 이 아니라 멘션·링크가 되지 않는다 — 이스케이프하면 `&lt;` 가 그대로
    # 보인다. 가림만 한 번 더 한다. 빈 칸은 공백 한 칸으로 둔다(값을 지어내지 않는다).
    masked = mask_sensitive(text).text.strip()
    return {"type": "raw_text", "text": masked or " "}


def render_blocks(phase: str, payload: dict, *, with_table: bool = True) -> list[dict]:
    """댓글 블록. 성공 단계에서 전사에 표가 있으면 글 블록 뒤에 표 블록 하나를 붙인다.

    표로 옮긴 줄은 글에서 빼고, 나머지 전사·설명·안내는 :func:`render` 와 같은 글이다.
    표가 없거나 ``with_table`` 이 거짓이면 :func:`render` 를 section 블록으로 감쌀 뿐이다.
    단계마다 블록을 늘 보낸다 — `chat.update` 에서 옛 표가 남지 않게.
    """
    transcript = tuple(payload.get("transcript") or ())[:MAX_TRANSCRIPT_LINES]
    span = table_span(transcript) if with_table and phase in _SUCCESS else None
    if span is not None:
        start, end, rows = span
        if any(contains_sensitive(cell) for row in rows for cell in row):
            span = None
    if span is None:
        return _sections(render(phase, payload))
    start, end, rows = span
    text = render(
        phase,
        payload,
        shown_transcript=transcript[:start] + transcript[end:],
        table_below=True,
    )
    width = len(rows[0])
    table = {
        "type": "table",
        "column_settings": [{"is_wrapped": True}] * width,
        "rows": [[_table_cell(cell) for cell in row] for row in rows],
    }
    return [*_sections(text), table]


# ---------------------------------------------------------------------------
# 전진 규칙 — 늦게 온 옛 상태가 새 상태를 덮지 않는다
# ---------------------------------------------------------------------------


def advance(current: NoticeRow | None, incoming: NoticeRow) -> bool:
    """worker 단계 ``incoming`` 이 ``current`` 를 대신해야 하나.

    - 처음이면 받는다
    - 같은 job(둘 다 job 이 없는 경우 포함)이면 순위가 오를 때만
    - 다른, 더 나중에 만든 job 이면 받는다(재처리)
    - job 없는 보류·실패는 성공 결과를 덮지 않는다

    `accepted` 는 여기서 판정하지 않는다 — 단계가 아니라 「Slack 대상」 표시다(:func:`resolve`).
    """
    if incoming.phase == ACCEPTED:
        return current is None
    if current is None:
        return True
    if incoming.job_id == current.job_id:
        return RANK[incoming.phase] > RANK[current.phase]
    if not incoming.job_id:
        # 예약 전에 멈춘 보류·실패. 끝난 job 의 보류·실패만 바꾼다(재처리 대기).
        # 성공·처리 중은 덮지 않는다. 한 번 바뀌면 job 이 같아져 같은 판정이 반복되지 않는다.
        return current.phase in {DEFERRED, FAILED}
    if not current.job_id:
        return True
    if incoming.job_created_at is None or current.job_created_at is None:
        return False
    return incoming.job_created_at > current.job_created_at


def resolve(current: NoticeRow | None, incoming: NoticeRow) -> NoticeRow | None:
    """새 상태 행. 바꿀 것이 없으면 ``None``.

    **접수(`accepted`)는 Archiver 의 실시간 수집에서만 온다** — 이 행을 Slack 에 보여도
    된다는 표시(`slack_eligible`)를 켠다. 소급·과거 아카이브 이미지는 접수가 없으므로 worker
    가 처리해도 DB·콘솔에만 남고 댓글은 생기지 않는다.

    worker 완료가 접수보다 먼저 적혔어도(경합) 완료를 잃지 않는다. 그때 접수는 단계를
    되돌리지 않고 대상 표시만 켜며, 비어 있는 파일명·기본 OCR 만 채운다.
    """
    if incoming.phase == ACCEPTED:
        if current is None:
            return replace(
                incoming,
                version=1,
                delivered_version=0,
                slack_eligible=True,
                attempt_version=0,
                delivery_state="pending",
            )
        if current.slack_eligible:
            return None
        payload = dict(current.payload)
        for name, value in incoming.payload.items():
            if name not in payload or payload[name] in ("", None):
                payload[name] = value
        return replace(current, slack_eligible=True, payload=payload, version=current.version + 1)
    if not advance(current, incoming):
        return None
    if current is None:
        return replace(
            incoming,
            version=1,
            delivered_version=0,
            slack_eligible=False,
            attempt_version=0,
            delivery_state="pending",
        )
    payload = dict(current.payload) | dict(incoming.payload)
    if incoming.phase not in _SUCCESS:
        for name in (
            "description",
            "tags",
            "transcript",
            "transcript_more",
            "search_published",
            "provider",
        ):
            payload.pop(name, None)
    if incoming.phase not in {FAILED, DEFERRED}:
        payload.pop("reason", None)
    # worker 가 바꾸는 칸은 여기까지다. 전달 칸(reply_ts·delivery_state·시도)은 Archiver 몫이고,
    # 새 판은 `attempt_version < version` 으로 「아직 시도 안 함」 이 된다(:func:`effective`).
    return replace(
        current,
        phase=incoming.phase,
        job_id=incoming.job_id,
        job_created_at=incoming.job_created_at,
        payload=payload,
        version=current.version + 1,
    )


def merged(current: NoticeRow | None, incoming: NoticeRow) -> NoticeRow:
    """옛 이름. :func:`resolve` 가 바꿀 것이 있다고 할 때만 부른다."""
    result = resolve(current, incoming)
    if result is None:
        raise NoticeError("nothing_to_merge")
    return result


def effective(row: NoticeRow) -> NoticeRow:
    """전달 칸을 **현재 판** 기준으로 읽는다.

    worker 가 새 판을 적어도 전달 칸은 건드리지 않는다(권한도 없다). 그래서 시도 판이 현재
    판보다 낮으면 그 전달 칸은 옛 판의 것이고, 현재 판은 「보낼 차례, 시도 0회」 다.
    """
    if row.version == row.delivered_version:
        return replace(row, delivery_state="delivered") if row.reply_ts else row
    if row.attempt_version < row.version:
        return replace(
            row, delivery_state="pending", attempts=0, error_code="", next_attempt_at=None
        )
    return row


# ---------------------------------------------------------------------------
# 저장소 — PostgreSQL. 시험은 같은 규칙을 쓰는 메모리 판을 쓴다
# ---------------------------------------------------------------------------

_COLUMNS = (
    "workspace, channel_id, message_ts, file_id, phase, job_id, job_created_at, payload, "
    "version, slack_eligible, delivered_version, reply_ts, delivery_state, attempt_version, "
    "attempts, last_attempt_at, next_attempt_at, error_code"
)
_WHERE_KEY = "workspace=%s AND channel_id=%s AND message_ts=%s AND file_id=%s"


def _key_args(key: NoticeKey) -> tuple[str, str, str, str]:
    return key.workspace, key.channel_id, key.message_ts, key.file_id


def _row(data: dict) -> NoticeRow:
    payload = data.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return NoticeRow(
        key=NoticeKey(
            str(data["workspace"]),
            str(data["channel_id"]),
            str(data["message_ts"]),
            str(data["file_id"]),
        ),
        phase=str(data["phase"]),
        job_id=str(data.get("job_id") or ""),
        job_created_at=data.get("job_created_at"),
        payload=dict(payload),
        version=int(data["version"]),
        slack_eligible=bool(data.get("slack_eligible")),
        delivered_version=int(data["delivered_version"]),
        reply_ts=str(data.get("reply_ts") or ""),
        delivery_state=str(data["delivery_state"]),
        attempt_version=int(data.get("attempt_version") or 0),
        attempts=int(data.get("attempts") or 0),
        last_attempt_at=data.get("last_attempt_at"),
        next_attempt_at=data.get("next_attempt_at"),
        error_code=str(data.get("error_code") or ""),
    )


def connect(url: str | None = None):
    """상태 표 전용 연결. **autocommit** 이다 — 「시도 시작」 이 게시 전에 확정돼야
    게시 뒤 프로세스가 죽어도 다음 시도가 스레드에서 앞선 댓글을 먼저 찾는다."""
    import os

    import psycopg

    dsn = url or os.getenv("DATABASE_URL", "")
    if not dsn:
        raise NoticeError("database_unconfigured")
    return psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row)


class PostgresNoticeRepo:
    """`image_status_notice` 표.

    판정은 행 잠금(`FOR UPDATE`) 아래에서 :func:`resolve` 가 한다. 처음 쓰는 두 기록
    (worker 완료와 Archiver 접수)이 동시에 INSERT 하면 하나는 충돌로 빠지고, **다시 잠가서**
    방금 생긴 행과 합친다 — 어느 쪽도 잃지 않는다.

    worker(`tybot_vision`)는 단계 칸만 쓴다. 스키마가 열 권한으로 그것을 강제한다.
    """

    def __init__(self, connection) -> None:
        self.connection = connection

    def _job_created_at(self, cur, job_id: str) -> datetime | None:
        if not job_id:
            return None
        cur.execute("SELECT created_at FROM archiver_image_description_job WHERE id=%s", (job_id,))
        found = cur.fetchone()
        return found["created_at"] if found else None

    def record(self, incoming: NoticeRow) -> bool:
        from psycopg.types.json import Jsonb

        key = incoming.key
        for _ in range(2):
            with self.connection.transaction(), self.connection.cursor() as cur:
                if incoming.job_id and incoming.job_created_at is None:
                    incoming = replace(
                        incoming, job_created_at=self._job_created_at(cur, incoming.job_id)
                    )
                cur.execute(
                    f"SELECT {_COLUMNS} FROM image_status_notice WHERE {_WHERE_KEY} FOR UPDATE",
                    _key_args(key),
                )
                found = cur.fetchone()
                current = _row(found) if found else None
                row = resolve(current, incoming)
                if row is None:
                    return False
                if current is None:
                    if row.slack_eligible:
                        cur.execute(
                            "INSERT INTO image_status_notice (workspace, channel_id, "
                            "message_ts, file_id, phase, job_id, job_created_at, payload, "
                            "slack_eligible) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true) "
                            "ON CONFLICT DO NOTHING",
                            (
                                *_key_args(key),
                                row.phase,
                                row.job_id,
                                row.job_created_at,
                                Jsonb(row.payload),
                            ),
                        )
                    else:
                        # worker 는 slack_eligible 열에 INSERT 권한이 없다(기본값 false).
                        cur.execute(
                            "INSERT INTO image_status_notice (workspace, channel_id, "
                            "message_ts, file_id, phase, job_id, job_created_at, payload) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (
                                *_key_args(key),
                                row.phase,
                                row.job_id,
                                row.job_created_at,
                                Jsonb(row.payload),
                            ),
                        )
                    if cur.rowcount == 1:
                        return True
                    continue  # 다른 기록이 먼저 만들었다 — 다시 잠가서 합친다
                if row.slack_eligible and not current.slack_eligible:
                    cur.execute(
                        "UPDATE image_status_notice SET slack_eligible=true, payload=%s, "
                        f"version=%s, updated_at=now() WHERE {_WHERE_KEY}",
                        (Jsonb(row.payload), row.version, *_key_args(key)),
                    )
                else:
                    cur.execute(
                        "UPDATE image_status_notice SET phase=%s, job_id=%s, "
                        "job_created_at=%s, payload=%s, version=%s, updated_at=now() "
                        f"WHERE {_WHERE_KEY}",
                        (
                            row.phase,
                            row.job_id,
                            row.job_created_at,
                            Jsonb(row.payload),
                            row.version,
                            *_key_args(key),
                        ),
                    )
                return True
        raise NoticeError("record_conflict")

    def pending(self, workspace: str, *, limit: int = 20) -> list[NoticeRow]:
        with self.connection.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM image_status_notice "
                "WHERE workspace=%s AND slack_eligible AND version > delivered_version "
                "AND (attempt_version < version "
                "     OR (delivery_state IN ('pending','retrying') AND next_attempt_at <= now())) "
                "ORDER BY updated_at LIMIT %s",
                (workspace, limit),
            )
            return [effective(_row(dict(r))) for r in cur.fetchall()]

    def begin_attempt(self, row: NoticeRow) -> int:
        """시도를 **먼저** 센다. 게시 뒤 죽어도 다음 시도가 스레드를 먼저 찾게 한다."""
        with self.connection.cursor() as cur:
            cur.execute(
                "UPDATE image_status_notice SET "
                "attempts=CASE WHEN attempt_version=%s THEN attempts+1 ELSE 1 END, "
                "delivery_state=CASE WHEN attempt_version=%s THEN delivery_state "
                "                    ELSE 'pending' END, "
                "error_code=CASE WHEN attempt_version=%s THEN error_code ELSE '' END, "
                "attempt_version=%s, last_attempt_at=now(), updated_at=now() "
                f"WHERE {_WHERE_KEY} RETURNING attempts",
                (row.version, row.version, row.version, row.version, *_key_args(row.key)),
            )
            found = cur.fetchone()
            return int(found["attempts"]) if found else 0

    def mark_delivered(self, row: NoticeRow, reply_ts: str) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                "UPDATE image_status_notice SET reply_ts=%s, "
                "delivered_version=GREATEST(delivered_version, %s), "
                "attempt_version=GREATEST(attempt_version, %s), "
                "delivery_state='delivered', error_code='', updated_at=now() "
                f"WHERE {_WHERE_KEY}",
                (reply_ts, row.version, row.version, *_key_args(row.key)),
            )

    def mark_reply_ts(self, row: NoticeRow, reply_ts: str) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                f"UPDATE image_status_notice SET reply_ts=%s, updated_at=now() WHERE {_WHERE_KEY}",
                (reply_ts, *_key_args(row.key)),
            )

    def mark_retry(self, row: NoticeRow, code: str, next_at: datetime) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                "UPDATE image_status_notice SET delivery_state='retrying', error_code=%s, "
                f"next_attempt_at=%s, updated_at=now() WHERE {_WHERE_KEY} "
                "AND attempt_version=%s",
                (code, next_at, *_key_args(row.key), row.version),
            )

    def mark_failed(self, row: NoticeRow, code: str) -> None:
        with self.connection.cursor() as cur:
            cur.execute(
                "UPDATE image_status_notice SET delivery_state='failed_delivery', "
                "error_code=%s, attempt_version=GREATEST(attempt_version, %s), "
                f"updated_at=now() WHERE {_WHERE_KEY} AND version=%s",
                (code, row.version, *_key_args(row.key), row.version),
            )

    def recent(self, workspace: str, *, limit: int = 50) -> list[NoticeRow]:
        with self.connection.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM image_status_notice WHERE workspace=%s "
                "ORDER BY updated_at DESC LIMIT %s",
                (workspace, limit),
            )
            return [effective(_row(dict(r))) for r in cur.fetchall()]

    def resend(self, key: NoticeKey) -> bool:
        """콘솔의 「다시 보내기」. 콘솔(Vision 계정)은 전달 칸을 직접 고칠 권한이 없어
        좁은 함수(`image_notice_resend`, SECURITY DEFINER)를 부른다."""
        with self.connection.cursor() as cur:
            cur.execute("SELECT image_notice_resend(%s,%s,%s,%s) AS queued", _key_args(key))
            found = cur.fetchone()
            return bool(found and found["queued"])


class MemoryNoticeRepo:
    """시험용. 판정은 :func:`resolve`·:func:`effective` 를 그대로 쓰고, SQL 과 같은 칸만 고친다."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._rows: dict[NoticeKey, NoticeRow] = {}
        self.now = now or (lambda: datetime.now(UTC))

    @property
    def rows(self) -> dict[NoticeKey, NoticeRow]:
        return {key: effective(row) for key, row in self._rows.items()}

    def record(self, incoming: NoticeRow) -> bool:
        row = resolve(self._rows.get(incoming.key), incoming)
        if row is None:
            return False
        self._rows[incoming.key] = row
        return True

    def pending(self, workspace: str, *, limit: int = 20) -> list[NoticeRow]:
        now = self.now()
        due = []
        for row in self._rows.values():
            if (
                row.key.workspace != workspace
                or not row.slack_eligible
                or row.version <= row.delivered_version
            ):
                continue
            fresh = row.attempt_version < row.version
            waiting = row.delivery_state in {"pending", "retrying"} and (
                row.next_attempt_at is None or row.next_attempt_at <= now
            )
            if fresh or waiting:
                due.append(effective(row))
        return due[:limit]

    def begin_attempt(self, row: NoticeRow) -> int:
        current = self._rows[row.key]
        same = current.attempt_version == row.version
        updated = replace(
            current,
            attempts=current.attempts + 1 if same else 1,
            delivery_state=current.delivery_state if same else "pending",
            error_code=current.error_code if same else "",
            attempt_version=row.version,
            last_attempt_at=self.now(),
        )
        self._rows[row.key] = updated
        return updated.attempts

    def mark_delivered(self, row: NoticeRow, reply_ts: str) -> None:
        current = self._rows[row.key]
        self._rows[row.key] = replace(
            current,
            reply_ts=reply_ts,
            delivered_version=max(current.delivered_version, row.version),
            attempt_version=max(current.attempt_version, row.version),
            delivery_state="delivered",
            error_code="",
        )

    def mark_reply_ts(self, row: NoticeRow, reply_ts: str) -> None:
        self._rows[row.key] = replace(self._rows[row.key], reply_ts=reply_ts)

    def mark_retry(self, row: NoticeRow, code: str, next_at: datetime) -> None:
        current = self._rows[row.key]
        if current.attempt_version == row.version:
            self._rows[row.key] = replace(
                current, delivery_state="retrying", error_code=code, next_attempt_at=next_at
            )

    def mark_failed(self, row: NoticeRow, code: str) -> None:
        current = self._rows[row.key]
        if current.version == row.version:
            self._rows[row.key] = replace(
                current,
                delivery_state="failed_delivery",
                error_code=code,
                attempt_version=max(current.attempt_version, row.version),
            )

    def recent(self, workspace: str, *, limit: int = 50) -> list[NoticeRow]:
        return [r for r in self.rows.values() if r.key.workspace == workspace][:limit]

    def resend(self, key: NoticeKey) -> bool:
        current = self._rows.get(key)
        if (
            current is None
            or not current.slack_eligible
            or current.version <= current.delivered_version
            or current.attempt_version != current.version
            or current.delivery_state not in {"failed_delivery", "retrying"}
        ):
            return False
        self._rows[key] = replace(
            current, delivery_state="pending", attempts=0, error_code="", next_attempt_at=None
        )
        return True


# ---------------------------------------------------------------------------
# worker 쪽 — Vision 결과를 상태로 적는다(Slack 없음)
# ---------------------------------------------------------------------------


def worker_phase(state: str, code: str) -> tuple[str, str] | None:
    """코디네이터 결과 → (단계, 실패 사유). 알릴 것이 없으면 ``None``."""
    if code in SILENT_CODES:
        return None
    if state == "published":
        return None  # 성공은 revision 을 읽어 completed/uncertain 으로 적는다
    if code in RETRY_NEXT_BATCH:
        return DEFERRED, "provider_unavailable"
    if code in RETRY_AFTER_LIMIT:
        return DEFERRED, "limit"
    if code in PROVIDER_TRANSIENT:
        return FAILED, "provider_error"
    if state == "closed_without_revision":
        return FAILED, "unusable_output"
    if state in {"blocked", "unknown", "approval_required"}:
        return FAILED, "blocked"
    return None


def load_success_payload(connection, revision_id: str) -> tuple[str, dict]:
    """발행된 revision 에서 단계와 보여 줄 값을 만든다. 저장된 가드 통과값만 읽는다."""
    if not _JOB.fullmatch(str(revision_id or "")):
        raise NoticeError("revision_invalid")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT status, description, tags, provider FROM "
            "archiver_image_description_revision WHERE id=%s",
            (revision_id,),
        )
        described = cur.fetchone()
        if not described:
            raise NoticeError("revision_missing")
        cur.execute(
            "SELECT id, status, body FROM archiver_image_transcript_revision "
            "WHERE description_revision_id=%s LIMIT 1",
            (revision_id,),
        )
        transcribed = cur.fetchone() or {}
        revision_ids = [revision_id] + ([transcribed["id"]] if transcribed.get("id") else [])
        cur.execute(
            "SELECT count(*) AS n FROM image_search_entry WHERE revision_id = ANY(%s)",
            (revision_ids,),
        )
        published = int((cur.fetchone() or {}).get("n") or 0)
    statuses = {str(described.get("status") or ""), str(transcribed.get("status") or "")}
    fields = recognition_fields(
        description=str(described.get("description") or ""),
        tags=tuple(described.get("tags") or ()),
        transcript=str(transcribed.get("body") or ""),
        uncertain="uncertain" in statuses,
    )
    phase = UNCERTAIN if fields.pop("uncertain") else COMPLETED
    payload = {
        "description": fields["description"],
        "tags": list(fields["tags"]),
        "transcript": list(fields["transcript"]),
        "transcript_more": fields["transcript_more"],
        "provider": str(described.get("provider") or ""),
        # 설명과(있다면) 전사가 **둘 다** 검색 투영에 있어야 「반영 완료」 다.
        "search_published": published >= len(revision_ids),
    }
    return phase, payload


def doc_payload(doc) -> dict:
    """정본 첨부 문서에서 파일 이름·기본 OCR 상태. 접수를 놓친 이미지도 이름이 보이게."""
    if doc is None:
        return {}
    return {
        "file": file_label(getattr(doc, "name", "")),
        "ocr": ocr_state(
            str(getattr(doc, "conversion_state", "") or ""),
            bool(str(getattr(doc, "text", "") or "").strip()),
        ),
    }


def worker_status_recorder(repo) -> Callable:
    """코디네이터가 부르는 콜백. **절대 예외를 올리지 않는다** — 알림이 Vision 을 바꾸면 안 된다."""

    def record(item, *, phase: str = "", doc=None, connection=None) -> None:
        try:
            if not item.message_ts or not _CHANNEL.fullmatch(item.channel_id):
                return
            key = NoticeKey(item.workspace, item.channel_id, item.message_ts, item.file_id)
            payload = doc_payload(doc)
            reason = ""
            if not phase:
                if item.revision_id and connection is not None:
                    phase, success = load_success_payload(connection, item.revision_id)
                    payload |= success
                else:
                    decided = worker_phase(item.state, item.code)
                    if decided is None:
                        return
                    phase, reason = decided
            if reason:
                payload["reason"] = reason
            job_id = item.job_id if _JOB.fullmatch(str(item.job_id or "")) else ""
            repo.record(NoticeRow(key=key, phase=phase, job_id=job_id, payload=payload))
        except Exception:  # noqa: BLE001 - 상태 기록 실패가 Vision 결과를 바꾸지 않는다
            log.warning(
                "image status notice not recorded ws=%s channel=%s",
                getattr(item, "workspace", ""),
                getattr(item, "channel_id", ""),
            )

    return record


# ---------------------------------------------------------------------------
# Archiver 쪽 — Slack 에 한 댓글로 반영한다
# ---------------------------------------------------------------------------


def _slack_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = ""
    try:
        code = str((response or {}).get("error") or "")
    except Exception:  # noqa: BLE001 - 응답 모양이 달라도 본문은 싣지 않는다
        code = ""
    return safe_code(code) or "slack_unavailable"


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * (2 ** max(0, attempts - 1))))


def _find_existing_reply(client, row: NoticeRow, bot_user_id: str) -> str:
    """앞선 시도가 게시만 하고 `reply_ts` 를 못 적었을 때, 스레드에서 그 댓글을 찾는다."""
    response = client.conversations_replies(
        channel=row.key.channel_id,
        ts=row.key.message_ts,
        include_all_metadata=True,
        limit=200,
    )
    for message in response.get("messages") or ():
        metadata = message.get("metadata") or {}
        if metadata.get("event_type") != METADATA_EVENT:
            continue
        if (metadata.get("event_payload") or {}).get("key") != row.key.digest:
            continue
        if bot_user_id and message.get("user") not in {bot_user_id, None}:
            continue
        ts = str(message.get("ts") or "")
        if _TS.fullmatch(ts):
            return ts
    return ""


def deliver_one(
    client,
    repo,
    row: NoticeRow,
    *,
    allowed_channels,
    bot_user_id: str = "",
    now: Callable[[], datetime] | None = None,
) -> str:
    """한 행을 Slack 에 반영한다. 돌려주는 값은 결과 코드(로그·시험용)."""
    clock = now or (lambda: datetime.now(UTC))
    if row.key.channel_id not in allowed_channels:
        repo.mark_failed(row, "unregistered_channel")
        return "failed_delivery"
    attempts = repo.begin_attempt(row)
    # text 는 알림·접근성용 대체 글(표 행도 글 줄로 들어 있다). 보이는 것은 blocks 다.
    text = render(row.phase, row.payload)
    blocks = render_blocks(row.phase, row.payload)
    plain = render_blocks(row.phase, row.payload, with_table=False)
    metadata = {"event_type": METADATA_EVENT, "event_payload": {"key": row.key.digest}}

    def send(method, **kwargs):
        try:
            return method(text=text, blocks=blocks, metadata=metadata, **kwargs)
        except Exception as exc:
            if blocks == plain or _slack_error(exc) not in INVALID_BLOCK_ERRORS:
                raise
            # 표 블록을 이 워크스페이스가 받지 않는다 — 표 없이 같은 내용을 글로 보낸다.
            log.warning("[%s] image status table block rejected; sent as text", row.key.workspace)
            return method(text=text, blocks=plain, metadata=metadata, **kwargs)

    try:
        info = client.conversations_info(channel=row.key.channel_id).get("channel") or {}
        if not info.get("is_member"):
            repo.mark_failed(row, "not_member")
            return "failed_delivery"
        reply_ts = row.reply_ts
        if not reply_ts and attempts > 1:
            reply_ts = _find_existing_reply(client, row, bot_user_id)
            if reply_ts:
                repo.mark_reply_ts(row, reply_ts)
        if reply_ts:
            try:
                send(client.chat_update, channel=row.key.channel_id, ts=reply_ts)
            except Exception as exc:
                if _slack_error(exc) not in REPOST_SLACK_ERRORS:
                    raise
                reply_ts = ""
        if not reply_ts:
            posted = send(
                client.chat_postMessage,
                channel=row.key.channel_id,
                thread_ts=row.key.message_ts,
                unfurl_links=False,
                unfurl_media=False,
            )
            reply_ts = str(posted.get("ts") or "")
            if not _TS.fullmatch(reply_ts):
                raise NoticeError("slack_reply_ts_missing")
        repo.mark_delivered(row, reply_ts)
        return "delivered"
    except Exception as exc:  # noqa: BLE001 - 본문 없는 코드만 남긴다
        code = _slack_error(exc) if not isinstance(exc, NoticeError) else str(exc)
        if code in TERMINAL_SLACK_ERRORS:
            repo.mark_failed(row, code)
            return "failed_delivery"
        if attempts >= MAX_ATTEMPTS:
            repo.mark_failed(row, "retry_exhausted")
            return "failed_delivery"
        repo.mark_retry(row, code, clock() + _backoff(attempts))
        return "retrying"


def deliver_pending(
    client,
    repo,
    workspace: str,
    *,
    allowed_channels,
    bot_user_id: str = "",
    limit: int = 20,
    now: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    counts = {"delivered": 0, "retrying": 0, "failed_delivery": 0}
    for row in repo.pending(workspace, limit=limit):
        outcome = deliver_one(
            client,
            repo,
            row,
            allowed_channels=allowed_channels,
            bot_user_id=bot_user_id,
            now=now,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def console_row(row: NoticeRow) -> dict:
    """콘솔 표 한 줄. 본문·파일명 없이 좌표와 상태만."""
    row = effective(row)
    return {
        "channelId": row.key.channel_id,
        "messageTs": row.key.message_ts,
        "fileId": row.key.file_id,
        "phase": row.phase,
        "jobId": row.job_id,
        "slackEligible": row.slack_eligible,
        "deliveryState": row.delivery_state if row.slack_eligible else "not_announced",
        "version": row.version,
        "deliveredVersion": row.delivered_version,
        "attempts": row.attempts,
        "lastAttemptAt": row.last_attempt_at,
        "errorCode": safe_code(row.error_code),
        "hasReply": bool(row.reply_ts),
        "resendable": row.slack_eligible
        and row.delivery_state in {"failed_delivery", "retrying"}
        and row.version > row.delivered_version,
    }


__all__ = [
    "ACCEPTED",
    "COMPLETED",
    "DEFERRED",
    "FAILED",
    "PROCESSING",
    "UNCERTAIN",
    "VISION_FILETYPES",
    "MemoryNoticeRepo",
    "NoticeError",
    "NoticeKey",
    "NoticeRow",
    "PostgresNoticeRepo",
    "advance",
    "connect",
    "console_row",
    "deliver_one",
    "deliver_pending",
    "doc_payload",
    "effective",
    "load_success_payload",
    "merged",
    "receipt_payload",
    "render",
    "render_blocks",
    "resolve",
    "table_span",
    "worker_phase",
    "worker_status_recorder",
]
