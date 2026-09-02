"""사용자가 명시적으로 요청한 경우에만 독립 Slack Canvas로 답한다."""
from __future__ import annotations

import re
from dataclasses import dataclass

TITLE = "TYBot 정식 답변"
REQUEST_RE = re.compile(
    r"(?:캔버스로\s*(?:답변|작성)(?:해\s*줘|해주세요|해줘|해)?|"
    r"메시지\s*말고\s*정식\s*답변(?:해\s*줘|해주세요|해줘|해)?|"
    r"양식으로\s*답변(?:해\s*줘|해주세요|해줘|해)?)",
    re.IGNORECASE,
)
SLACK_LINK_RE = re.compile(r"<(?P<url>https?://[^>|]+)\|(?P<label>[^>]+)>")


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
    return f"# {TITLE}\n\n{converted}\n"


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
