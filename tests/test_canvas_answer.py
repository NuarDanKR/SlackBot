from unittest.mock import Mock

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
