from unittest.mock import Mock

import pytest

from tybot import canvas_answer
from tybot.canvas_answer import (
    DISCLAIMER,
    TITLE,
    answer_line_count,
    automatic,
    create,
    grant_channel,
    grant_user,
    markdown,
    parse_request,
)


def test_long_answers_use_canvas_unless_message_requested():
    from tybot.canvas_answer import automatic

    assert not automatic("x" * 5000, "정리해줘")
    assert not automatic("\n".join(["line"] * 25), "정리해줘")
    assert automatic("\n".join(["line"] * 26), "정리해줘")
    assert not automatic("short", "정리해줘")
    assert not automatic("\n".join(["line"] * 26), "메시지로 답변해줘")


def test_a_table_request_uses_canvas_even_when_the_model_returned_bullets():
    """표 요청은 모델이 Markdown 표를 만들었는지와 무관하게 Canvas로 보낸다."""
    bullets = "- 대우건설: 협의중\n- 케이씨씨건설: 수금완료"

    assert automatic(bullets, "수금 현황을 자세히 표로 그려줘")
    assert not automatic(bullets, "수금 현황을 메시지로 표로 알려줘")


def test_canvas_threshold_excludes_each_answers_source_block():
    first = "\n".join(["본문"] * 13) + "\n\n출처:\n" + "\n".join(["• 문서"] * 20)
    second = "\n".join(["본문"] * 12) + "\n\n출처:\n• 문서"
    reply = f"{first}\n\n───\n\n{second}"

    assert answer_line_count(reply) == 25
    assert not automatic(reply, "정리해줘")
    assert automatic(f"본문 한 줄\n───\n{reply}", "정리해줘")


def test_message_normalizes_headings_and_bold_but_preserves_code():
    from tybot.canvas_answer import message

    assert message("## 제목\n**강조**\n`**code**`\n```\n# code\n```") == (
        "*제목*\n*강조*\n`**code**`\n```\n# code\n```"
    )


def test_canvas_is_only_requested_by_explicit_phrases():
    assert parse_request("주간 현황을 캔버스로 답변해") == (True, "주간 현황을")
    assert parse_request("메시지 말고 정식 답변해 예산 현황") == (True, "예산 현황")
    assert parse_request("양식으로 답변해 공정 현황") == (True, "공정 현황")
    assert parse_request("주간 현황을 알려줘") == (False, "주간 현황을 알려줘")


def test_canvas_markdown_converts_slack_source_links():
    body = "*결론*\n답변\n\n출처:\n• <https://example.slack.com/F1|보고서 원본>"
    rendered = markdown(body)
    # 본문 첫 블록은 Disclaimer 다. **H1 제목은 넣지 않는다** — Slack 이 Canvas
    # 제목을 따로 보여 주므로 예전 형식은 같은 제목을 두 번 보여 줬다(설계 §4).
    assert rendered.startswith(DISCLAIMER)
    assert f"# {TITLE}" not in rendered
    assert "## 결론" in rendered
    assert "- [보고서 원본]" in rendered
    assert "[보고서 원본](https://example.slack.com/F1)" in rendered


def test_canvas_puts_the_users_question_before_the_answer():
    rendered = markdown(
        "답변 본문",
        question="지난주 기획팀에서 한 것들을 요약해줘",
    )

    assert "## 요청한 질문" in rendered
    assert rendered.index("지난주 기획팀에서 한 것들을 요약해줘") < rendered.index(
        "답변 본문"
    )


def test_vip_canvas_uses_vip_branding_only():
    rendered = markdown("답변 본문", bot_name="vipbot", question="현황 알려줘")

    assert "VIPBot이 요청 시점에" in rendered
    assert "TYBot이 요청 시점에" not in rendered


def test_canvas_markdown_keeps_normal_tables():
    table = "| 항목 | 금액 |\n| --- | ---: |\n| 기성 | 300 |"
    assert table in markdown(table)


def test_canvas_markdown_splits_tables_over_slacks_300_cell_limit():
    header = "| A | B | C |"
    separator = "| --- | --- | --- |"
    body = "\n".join(f"| {i} | x | y |" for i in range(120))
    rendered = markdown(f"{header}\n{separator}\n{body}")
    assert rendered.count(header) == 2
    assert rendered.count(separator) == 2
    assert "| 119 | x | y |" in rendered


def test_create_grants_current_conversation_and_returns_permalink():
    client = Mock()
    client.canvases_create.return_value = {"canvas_id": "F-CANVAS"}
    client.files_info.return_value = {
        "file": {"permalink": "https://example.slack.com/docs/F-CANVAS"}
    }

    result = create(client, "답변")

    assert result.canvas_id == "F-CANVAS"
    grant_channel(client, result.canvas_id, "C123")
    client.canvases_access_set.assert_called_once_with(
        canvas_id="F-CANVAS", access_level="read", channel_ids=["C123"]
    )
    grant_user(client, result.canvas_id, "U123")
    assert client.canvases_access_set.call_args.kwargs["user_ids"] == ["U123"]


def test_create_uses_the_vip_fallback_title_and_question():
    client = Mock()
    client.canvases_create.return_value = {"canvas_id": "F-VIP"}
    client.files_info.return_value = {
        "file": {"permalink": "https://example.slack.com/docs/F-VIP"}
    }

    create(client, "답변", bot_name="vipbot", question="무엇을 했어?")

    sent = client.canvases_create.call_args.kwargs
    assert sent["title"] == "VIPBot 정식 답변"
    assert "무엇을 했어?" in sent["document_content"]["markdown"]


# --- 섞인 중첩 목록 (2026-11-20 운영: canvas_creation_failed) -------------------

MIXED = """**사안별 보충**

1. **보증채무 현실화**: 같은 날 올라온 두 문서의 숫자가 다릅니다.
   - 사업 리스트는 구로 지산 632억으로 적었습니다.
   - PF 처리방안은 구로 634억으로 적었습니다.
2. **차입금**: 산업은행 한도 1,500억은 아직 쓰지 않았습니다.
"""


def _body_lists(md: str) -> list[str]:
    from tybot.canvas_answer import LIST_ITEM_RE

    return [line for line in md.splitlines() if LIST_ITEM_RE.match(line)]


def test_bullets_under_numbered_items_become_one_list_kind():
    from tybot.canvas_answer import _has_mixed_nesting

    md = canvas_answer.markdown(MIXED)

    assert not _has_mixed_nesting(md.splitlines())
    assert "- **1.** **보증채무 현실화**" in md
    assert "   - 사업 리스트는 구로 지산 632억으로 적었습니다." in md
    assert "- **2.** **차입금**" in md


def test_numbered_under_bullets_is_also_unmixed():
    from tybot.canvas_answer import _has_mixed_nesting

    md = canvas_answer.markdown("- 자금\n  1. 첫째\n  2. 둘째\n")

    assert not _has_mixed_nesting(md.splitlines())
    assert "  - **1.** 첫째" in md


def test_plain_numbered_lists_and_code_blocks_are_untouched():
    plain = "1. 하나\n2. 둘\n\n```\n1. 코드\n   - 그대로\n```\n"

    md = canvas_answer.markdown(plain)

    assert "1. 하나\n2. 둘" in md
    assert "```\n1. 코드\n   - 그대로\n```" in md


def test_create_retries_once_with_flat_lists_when_slack_rejects_the_markdown():
    class SlackError(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"error": code}

    client = Mock()
    client.canvases_create.side_effect = [
        SlackError("canvas_creation_failed"),
        {"canvas_id": "F-2"},
    ]
    client.files_info.return_value = {"file": {"permalink": "https://x/F-2"}}

    got = canvas_answer.create(client, MIXED)

    assert got.canvas_id == "F-2"
    second = client.canvases_create.call_args_list[1].kwargs["document_content"]["markdown"]
    assert not [line for line in _body_lists(second) if line.startswith((" ", "\t"))]
    assert "- · 사업 리스트는" in second


def test_create_does_not_retry_other_slack_errors():
    class SlackError(Exception):
        response = {"error": "not_authed"}  # noqa: RUF012

    client = Mock()
    client.canvases_create.side_effect = SlackError("x")

    with pytest.raises(SlackError):
        canvas_answer.create(client, MIXED)
    assert client.canvases_create.call_count == 1
