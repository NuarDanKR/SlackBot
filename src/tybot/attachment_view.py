"""첨부 검수 화면 — Slack 에서 보고 승인한다.

## 왜 Slack 인가

승인은 **원본을 벤더에 보내도 되는가** 를 정하는 일이다. 그러려면 그 파일을 봐야
하는데, 서버 목록에는 이름만 있다. 이름만 보고 승인하면 무엇을 내보내는지 모르는
채로 승인하게 된다 — 그건 게이트가 아니라 형식이다.

파일이 실제로 보이는 자리는 **그 파일이 올라온 채널**이다. 그래서 여기서 한다.

## 누가

채널 소유자 또는 그 채널의 요약 검토자(`channel_reviewer`). 아무나 승인하면
「승인 게이트」 가 이름만 남는다.

## 무엇을 보이나

이름·형식·크기와 **변환됐는지**. 변환된 파일은 이미 텍스트로 답변에 쓰이므로
승인이 급하지 않고, 변환이 안 되는 것(이미지·스캔·구형 hwp)은 **승인 없이는 내용을
알 방법이 없다.** 그 차이를 화면이 말해야 사람이 무엇부터 볼지 안다.
"""
from __future__ import annotations

from dataclasses import dataclass

# Block Kit 버튼의 `value` 는 **비우면 안 된다.** 비면 Slack 이 메시지를 통째로
# 거부하고, 그건 오류가 아니라 「아무 일도 안 일어남」 으로 나타난다(실제로 겪었다).
ACTION_APPROVE = "attachment_approve"
ACTION_REJECT = "attachment_reject"

# 한 화면에 보일 최대 건수. 넘으면 Slack 이 메시지를 자른다.
MAX_ITEMS = 10

EMPTY = "이 채널에는 검수 대기 중인 첨부가 없습니다."
DENIED = (
    "이 채널의 생성 요청자 또는 요약 검토자만 첨부를 승인할 수 있습니다.\n"
    "승인하면 그 원본이 LLM 제공자에게 전달됩니다."
)


@dataclass(frozen=True)
class Row:
    """화면 한 줄. 본문은 담지 않는다 — 이름과 상태뿐이다."""

    file_id: str
    name: str
    filetype: str
    size: int
    converted: bool
    convertible: bool

    @property
    def urgency(self) -> str:
        """무엇부터 봐야 하는지.

        변환된 파일은 이미 텍스트로 답변에 쓰인다 — 승인은 원본(표 이미지·서식)을
        추가로 보게 하는 것이라 급하지 않다. 변환이 안 되는 것은 승인 없이는
        **내용을 알 방법이 없다.**
        """
        if self.converted:
            return "텍스트는 이미 사용 중"
        if self.convertible:
            return "변환 실패 — 승인해야 읽습니다"
        return "변환 불가 — 승인해야 읽습니다"


# 버튼 `value` 는 **어느 채널의 어느 파일인지**를 함께 실어야 한다.
#
# 채널에서 누를 때는 Slack 이 `body["channel"]["id"]` 로 채널을 알려 준다. 그런데
# 같은 버튼을 **DM 으로 보내면** 그 값은 DM 채널(`D…`)이 되고, 처리부는 원본을
# 그 DM 채널에서 찾다가 0건으로 끝난다 — 오류가 아니라 「대상을 특정하지 못했습니다」다.
# 검토자에게 하루치를 밀어 주는 경로가 생기면서 이게 실제 경로가 됐다.
VALUE_SEP = "/"


def pack_value(channel_id: str, file_id: str) -> str:
    """버튼 값. **비우지 않는다** — 비면 Slack 이 메시지를 통째로 거부한다."""
    return f"{channel_id}{VALUE_SEP}{file_id}"


def unpack_value(raw: str) -> tuple[str, str]:
    """`(채널ID, 파일ID)`. 옛 형식(파일 ID 만)은 채널을 빈 문자열로 준다.

    이미 사람의 DM·채널에 떠 있는 옛 메시지의 버튼이 눌릴 수 있다. 그때 채널은
    호출부가 아는 값(`body`)으로 메운다.
    """
    text = (raw or "").strip()
    if VALUE_SEP in text:
        channel, _, file_id = text.partition(VALUE_SEP)
        return channel.strip(), file_id.strip()
    return "", text


def _kb(size: int) -> str:
    return f"{max(1, size // 1024):,}KB"


def rows_for(items, extracted: set[str]) -> list[Row]:
    """검수 대기 목록 → 화면 줄. `extracted` 는 변환본이 들어간 파일 이름."""
    from .archive.convert import can_convert

    out: list[Row] = []
    for item in items:
        suffix = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
        out.append(
            Row(
                file_id=item.file_id,
                name=item.name,
                filetype=item.filetype or suffix,
                size=item.size,
                converted=item.name in extracted,
                convertible=can_convert(suffix),
            )
        )
    # 승인 없이는 못 읽는 것부터. 사람의 시간을 거기 쓰게 한다.
    out.sort(key=lambda r: (r.converted, r.name))
    return out


def blocks(rows: list[Row], *, channel_name: str = "", channel_id: str = "") -> list[dict]:
    """검수 목록 Block Kit.

    **버튼 `value` 를 비우지 않는다.** 비면 Slack 이 메시지를 통째로 거부하고,
    오류가 아니라 「아무 일도 안 일어남」 으로 나타난다.
    """
    where = f" — {channel_name}" if channel_name else ""
    out: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*첨부 검수{where}*\n"
                    "승인하면 그 원본이 LLM 제공자에게 전달됩니다. "
                    "개인정보가 담긴 파일은 승인하지 마세요."
                ),
            },
        }
    ]
    for row in rows[:MAX_ITEMS]:
        out.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{row.name}*\n"
                        f"{row.filetype or '?'} · {_kb(row.size)} · {row.urgency}"
                    ),
                },
            }
        )
        out.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "승인"},
                        "style": "primary",
                        "action_id": ACTION_APPROVE,
                        "value": pack_value(channel_id, row.file_id),
                        "confirm": {
                            "title": {"type": "plain_text", "text": "원본을 내보냅니다"},
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"*{row.name}* 원본이 LLM 제공자에게 전달됩니다.\n"
                                    "되돌리려면 반려하면 되지만, **이미 보낸 것은 "
                                    "되돌아오지 않습니다.**"
                                ),
                            },
                            "confirm": {"type": "plain_text", "text": "승인"},
                            "deny": {"type": "plain_text", "text": "취소"},
                        },
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "반려"},
                        "action_id": ACTION_REJECT,
                        "value": pack_value(channel_id, row.file_id),
                    },
                ],
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
