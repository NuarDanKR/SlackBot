"""Explicit or long-form answers use Canvas; messages use Slack mrkdwn."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("tybot.canvas_answer")

# 예전 고정 제목. **새로 만들지 않는다** — 제목이 전부 같으면 Slack 목록에서
# 어느 문서인지 구별할 수 없었다. 수집 제외 판정에는 계속 쓴다(옛 문서가 남아 있다).
TITLE = "TYBot 정식 답변"
AUTO_CANVAS_CHARS = 1200
AUTO_CANVAS_LINES = 20

# 본문 **첫 블록**. 제목 자리가 아니라 여기에 둔다 — Slack 은 Canvas 제목을 따로
# 보여 주므로 본문 H1 은 같은 제목을 두 번 보여 줄 뿐이었다.
#
# 이 문장은 **생성 문서 표식**이기도 하다. 제목이 질문마다 달라지면 제목만으로는
# 우리 문서를 못 알아본다 — 그 순간 봇 답변이 다시 근거로 수집된다(원칙 1).
DISCLAIMER = (
    "> 이 문서는 TYBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서입니다.\n"
    "> 중요한 일정·금액·의사결정은 아래 출처 원문을 확인하세요."
)
# 수집 경로가 본문에서 찾을 조각. 문장 전체를 비교하면 줄바꿈 한 번에 어긋난다.
DISCLAIMER_MARK = "TYBot이 요청 시점에 확인 가능한 사내 자료를 바탕으로 생성한 AI 문서"
# 제목 접미사. **한 곳에서만 정의한다** — 만드는 쪽과 제외하는 쪽이 다른 값을
# 들고 있으면 우리가 만든 문서를 우리가 다시 수집한다(원칙 1).
TITLE_SUFFIX = " · TYBot"


def automatic(body: str, request: str) -> bool:
    if re.search(r"(메시지로|메시지에|캔버스\s*(말고|쓰지|사용하지))", request):
        return False
    return (len(body.strip()) >= AUTO_CANVAS_CHARS
            or len([line for line in body.splitlines() if line.strip()]) >= AUTO_CANVAS_LINES)


def message(body: str) -> str:
    """Normalize Markdown prose without rewriting fenced or inline code."""
    from .evidence_view import fix_markdown_tables

    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", body)
    for index in range(0, len(parts), 2):
        text = parts[index]
        if "|" in text:
            leading = text[:len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()):]
            text = leading + fix_markdown_tables(text).strip() + trailing
        text = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$", r"*\1*", text)
        text = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", text)
        parts[index] = text
    return "".join(parts)
REQUEST_RE = re.compile(
    r"(?:캔버스로\s*(?:답변|작성)(?:해\s*줘|해주세요|해줘|해)?|"
    r"메시지\s*말고\s*정식\s*답변(?:해\s*줘|해주세요|해줘|해)?|"
    r"양식으로\s*답변(?:해\s*줘|해주세요|해줘|해)?)",
    re.IGNORECASE,
)
SLACK_LINK_RE = re.compile(r"<(?P<url>https?://[^>|]+)\|(?P<label>[^>]+)>")
TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
MAX_TABLE_CELLS = 300


@dataclass(frozen=True)
class CanvasResult:
    canvas_id: str
    permalink: str


def parse_request(text: str) -> tuple[bool, str]:
    """명시적 Canvas 지시를 제거해 검색어를 오염시키지 않는다."""
    matched = bool(REQUEST_RE.search(text or ""))
    cleaned = REQUEST_RE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.!?\t\r\n")
    return matched, cleaned


def markdown(body: str) -> str:
    """Slack mrkdwn을 Canvas용 Markdown으로 바꾸고 답변·근거 구조를 보존한다.

    **본문에 H1 제목을 넣지 않는다.** Slack 이 Canvas 제목을 따로 보여 주므로
    예전 형식은 같은 제목을 두 번 보여 줬다. 대신 첫 블록에 Disclaimer 를 둔다.
    """
    converted = SLACK_LINK_RE.sub(r"[\g<label>](\g<url>)", body.strip())
    converted = re.sub(r"(?m)^•\s+", "- ", converted)
    converted = re.sub(r"(?m)^\*([^*\n]+)\*:?\s*$", r"## \1", converted)
    converted = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", converted)
    converted = _split_large_tables(converted)
    return f"{DISCLAIMER}\n\n{converted}\n"


def _table_cells(line: str) -> list[str]:
    """Split a conventional Markdown table row, preserving escaped pipes."""
    marker = "\x00TYBOT_PIPE\x00"
    protected = line.strip().replace(r"\|", marker).strip("|")
    return [cell.strip().replace(marker, r"\|") for cell in protected.split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells)


def _split_large_tables(text: str) -> str:
    """Keep each Canvas Markdown table within Slack's 300-cell limit."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or "|" not in lines[index] or not _is_table_separator(lines[index + 1]):
            out.append(lines[index])
            index += 1
            continue
        header, separator = lines[index], lines[index + 1]
        columns = len(_table_cells(header))
        data: list[str] = []
        cursor = index + 2
        while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
            data.append(lines[cursor])
            cursor += 1
        # The header counts as one row. Tables wider than the API limit cannot
        # be split by rows without changing their meaning, so Canvas creation
        # will fail and the existing readable-message fallback will be used.
        rows_per_table = (MAX_TABLE_CELLS // columns) - 1 if columns else 0
        if rows_per_table < 1 or (len(data) + 1) * columns <= MAX_TABLE_CELLS:
            out.extend([header, separator, *data])
        else:
            for start in range(0, len(data), rows_per_table):
                if start:
                    out.append("")
                out.extend([header, separator, *data[start:start + rows_per_table]])
        index = cursor
    return "\n".join(out)


def create(
    client, body: str, *, title: str = "", provenance: dict | None = None
) -> CanvasResult:
    """독립 Canvas를 만들고 링크를 반환한다. 공유 범위는 호출자가 별도로 설정한다.

    `title` 은 이미 검증된 값이다(`master_planner.artifact_for`). 비어 있으면
    예전 고정 제목으로 돌아간다 — **여기서 제목을 지어내지 않는다.**

    `provenance` 는 `{workspace, channel_id, qa_record_id}`. 생성한 Canvas 의
    ID 를 기록해 **다시 수집하지 않게** 한다(원칙 1). 기록에 실패해도 Canvas 는
    만든다 — 제목 접미사와 Disclaimer 가 남은 방어선이다.
    """
    response = client.canvases_create(
        title=title.strip() or TITLE,
        document_content={"type": "markdown", "markdown": markdown(body)},
    )
    canvas_id = str(response.get("canvas_id") or response.get("file_id") or "")
    if not canvas_id:
        raise RuntimeError("Slack canvases.create 응답에 canvas_id가 없습니다")
    info = client.files_info(file=canvas_id)
    permalink = str((info.get("file") or {}).get("permalink") or "")
    if not permalink:
        raise RuntimeError("생성된 Canvas의 permalink를 찾지 못했습니다")
    if provenance:
        remember_generated(canvas_id, **provenance)
    return CanvasResult(canvas_id, permalink)


# --- 생성 Canvas 기록 ---------------------------------------------------------
#
# 동적 제목을 쓰면 `title.startswith("TYBot 정식 답변")` 검사가 더 이상 우리
# 문서를 못 알아본다. 그 검사 하나에 기대고 있던 것이 **원칙 1(요약 재귀 금지)**
# 이라, 제목을 바꾸기 전에 방어선을 셋으로 늘린다.
#
#   1. 제목 접미사 ` · TYBot`
#   2. 본문 첫 블록의 Disclaimer
#   3. 여기 기록한 canvas_id
#
# 셋 중 **하나만 맞아도** 제외한다. 기록은 유실될 수 있고(디스크 오류·재설치),
# 제목은 사람이 고칠 수 있다. 한 겹으로는 못 막는다.
GENERATED_LOG = "generated-canvases.jsonl"
# 한 파일에 무한히 쌓이면 읽는 쪽이 느려진다. 최근 것부터 이만큼만 읽는다.
MAX_GENERATED_ROWS = 20_000


def generated_log_path() -> Path:
    from .heartbeat import state_dir

    return state_dir() / GENERATED_LOG


def remember_generated(
    canvas_id: str, *, workspace: str = "", channel_id: str = "", qa_record_id: str = ""
) -> bool:
    """생성한 Canvas 의 좌표만 남긴다. **제목도 본문도 넣지 않는다.**

    실패해도 예외를 올리지 않는다 — 기록 실패로 답변을 못 보내면 그게 더 나쁘다.
    """
    if not canvas_id:
        return False
    row = {
        "canvas_id": canvas_id,
        "workspace": workspace,
        "channel_id": channel_id,
        "qa_record_id": qa_record_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        path = generated_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        log.warning("생성 Canvas 기록 실패 %s: %s", canvas_id, exc)
        return False


def is_generated(canvas_id: str) -> bool:
    """우리가 만든 Canvas 인가. 기록을 못 읽으면 **False** 다.

    여기서 True 로 기울면 사람이 만든 Canvas 가 수집에서 빠진다 — 자료가 조용히
    사라지는 쪽이 중복 수집보다 나쁘다. 못 읽었을 때의 방어선은 제목과
    Disclaimer 다.
    """
    if not canvas_id:
        return False
    try:
        path = generated_log_path()
        if not path.exists():
            return False
        with path.open(encoding="utf-8") as fh:
            rows = fh.readlines()[-MAX_GENERATED_ROWS:]
    except OSError as exc:
        log.warning("생성 Canvas 기록을 읽지 못했습니다: %s", exc)
        return False
    needle = f'"canvas_id": "{canvas_id}"'
    return any(needle in line for line in rows)


def grant_channel(client, canvas_id: str, channel_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", channel_ids=[channel_id]
    )


def grant_user(client, canvas_id: str, user_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", user_ids=[user_id]
    )
