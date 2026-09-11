"""첨부 처리 화면 — 자동 변환 결과와 실패를 Slack에서 확인한다.

## 왜 Slack 인가

문서 변환은 수집 시점에 자동으로 실행한다. 성공한 변환본은 PII 검사를 통과한 뒤
아카이브에 들어가고, 실패한 원본은 격리 보관한다.

## 누가

채널 담당자와 그 채널의 요약 검토자(`channel_reviewer`)에게 정해진 시각에 보낸다.

## 무엇을 보이나

이름·형식·크기, 자동 변환 결과와 PII 검사를 통과한 변환본의 짧은 미리보기를 보인다.
변환 실패에는 실패 사유와 Slack 원본 링크를 붙인다. 원본을 외부 LLM에 보내는 승인
통로는 제공하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

# 한 화면에 보일 최대 건수. 넘으면 Slack 이 메시지를 자른다.
MAX_ITEMS = 10

EMPTY = "이 채널에는 확인할 첨부 처리 내역이 없습니다."


@dataclass(frozen=True)
class Row:
    """화면 한 줄. 원본은 담지 않고 검사 완료된 변환본만 짧게 담는다."""

    file_id: str
    name: str
    filetype: str
    size: int
    converted: bool
    convertible: bool
    status: str = "pending_review"
    preview: str = ""
    failure_reason: str = ""
    permalink: str = ""

    @property
    def urgency(self) -> str:
        """자동 변환 결과와 필요한 운영 조치를 설명한다."""
        if self.converted:
            return "자동 변환 완료 · 텍스트는 이미 사용 중"
        if self.failure_reason:
            return "변환 실패"
        if self.convertible:
            return "자동 변환 실패 — 원본 확인 필요"
        return "지원하지 않는 형식 — 원본 확인 필요"


def _kb(size: int) -> str:
    return f"{max(1, size // 1024):,}KB"


def rows_for(items, extracted: set[str]) -> list[Row]:
    """첨부 처리 목록 → 화면 줄. `extracted` 는 변환본이 들어간 파일 이름."""
    from .archive.convert import can_convert
    from .attachment_review import extracted_preview, public_failure_reason

    out: list[Row] = []
    for item in items:
        suffix = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
        out.append(
            Row(
                file_id=item.file_id,
                name=item.name,
                filetype=item.filetype or suffix,
                size=item.size,
                converted=bool(getattr(item, "extracted", False)) or item.name in extracted,
                convertible=can_convert(suffix),
                status=str(getattr(item, "status", "pending_review")),
                preview=extracted_preview(item) if getattr(item, "extracted", False) else "",
                failure_reason=(
                    public_failure_reason(item)
                    if getattr(item, "conversion_failed", False)
                    else ""
                ),
                permalink=str(getattr(item, "permalink", "") or ""),
            )
        )
    # 읽지 못한 것부터 보여 운영자가 먼저 조치할 수 있게 한다.
    out.sort(key=lambda r: (r.converted, r.name))
    return out


def blocks(rows: list[Row], *, channel_name: str = "", channel_id: str = "") -> list[dict]:
    """첨부 자동 변환 결과 Block Kit."""
    where = f" — {channel_name}" if channel_name else ""
    out: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*첨부 처리 현황{where}*\n"
                    "문서 변환은 자동으로 진행됩니다. 실패한 원본은 외부 LLM에 "
                    "전송하지 않으며, Slack 원본을 확인한 뒤 변환 환경을 조치합니다."
                ),
            },
        }
    ]
    for row in rows[:MAX_ITEMS]:
        detail = f"{row.filetype or '?'} · {_kb(row.size)} · {row.urgency}"
        if row.failure_reason:
            detail += f"\n{row.failure_reason}"
        if row.permalink:
            detail += f" · <{row.permalink}|Slack 원본>"
        if row.preview:
            safe_preview = (
                row.preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            safe_preview = safe_preview.replace("```", "'''")
            detail += f"\n```{safe_preview}```"
        out.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{row.name}*\n{detail}",
                },
            }
        )
    if len(rows) > MAX_ITEMS:
        out.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"외 {len(rows) - MAX_ITEMS}건. 처리하면 다음이 보입니다.",
                    }
                ],
            }
        )
    return out
