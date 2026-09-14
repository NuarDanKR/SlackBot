"""Explicit or long-form answers use Canvas; messages use Slack mrkdwn."""
from __future__ import annotations

import re
from dataclasses import dataclass

TITLE = "TYBot 정식 답변"
AUTO_CANVAS_CHARS = 1200
AUTO_CANVAS_LINES = 20


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
    """Slack mrkdwn을 Canvas용 Markdown으로 바꾸고 답변·근거 구조를 보존한다."""
    converted = SLACK_LINK_RE.sub(r"[\g<label>](\g<url>)", body.strip())
    converted = re.sub(r"(?m)^•\s+", "- ", converted)
    converted = re.sub(r"(?m)^\*([^*\n]+)\*:?\s*$", r"## \1", converted)
    converted = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", converted)
    converted = _split_large_tables(converted)
    return f"# {TITLE}\n\n{converted}\n"


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


def create(client, body: str) -> CanvasResult:
    """독립 Canvas를 만들고 링크를 반환한다. 공유 범위는 호출자가 별도로 설정한다."""
    response = client.canvases_create(
        title=TITLE,
        document_content={"type": "markdown", "markdown": markdown(body)},
    )
    canvas_id = str(response.get("canvas_id") or response.get("file_id") or "")
    if not canvas_id:
        raise RuntimeError("Slack canvases.create 응답에 canvas_id가 없습니다")
    info = client.files_info(file=canvas_id)
    permalink = str((info.get("file") or {}).get("permalink") or "")
    if not permalink:
        raise RuntimeError("생성된 Canvas의 permalink를 찾지 못했습니다")
    return CanvasResult(canvas_id, permalink)


def grant_channel(client, canvas_id: str, channel_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", channel_ids=[channel_id]
    )


def grant_user(client, canvas_id: str, user_id: str) -> None:
    client.canvases_access_set(
        canvas_id=canvas_id, access_level="read", user_ids=[user_id]
    )
