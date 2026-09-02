from unittest.mock import Mock

from tybot.canvas_answer import TITLE, create, grant_channel, grant_user, markdown, parse_request


def test_canvas_is_only_requested_by_explicit_phrases():
    assert parse_request("주간 현황을 캔버스로 답변해") == (True, "주간 현황을")
    assert parse_request("메시지 말고 정식 답변해 예산 현황") == (True, "예산 현황")
    assert parse_request("양식으로 답변해 공정 현황") == (True, "공정 현황")
    assert parse_request("주간 현황을 알려줘") == (False, "주간 현황을 알려줘")


def test_canvas_markdown_converts_slack_source_links():
    body = "*결론*\n답변\n\n출처:\n• <https://example.slack.com/F1|보고서 원본>"
    rendered = markdown(body)
    assert rendered.startswith(f"# {TITLE}")
    assert "## 결론" in rendered
    assert "- [보고서 원본]" in rendered
    assert "[보고서 원본](https://example.slack.com/F1)" in rendered


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
