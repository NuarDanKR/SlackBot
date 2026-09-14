from unittest.mock import Mock

from tybot.slack.progress import RequestProgress


def make():
    client = Mock()
    client.chat_update.return_value = {"ts": "notice"}
    say = Mock(return_value={"ts": "notice"})
    return RequestProgress(client, say, channel="C1", root_ts="root"), client, say


def test_fast_completion_never_posts_notice():
    progress, client, say = make()
    progress.send(text="answer")
    progress.show()
    say.assert_called_once_with(text="answer")
    client.chat_update.assert_not_called()


def test_slow_request_updates_same_notice():
    progress, client, say = make()
    progress.show()
    progress.show()
    assert progress.send(text="answer") == {"ts": "notice"}
    say.assert_called_once()
    assert say.call_args.kwargs["thread_ts"] == "root"
    client.chat_update.assert_called_once_with(
        channel="C1", ts="notice", text="answer", blocks=[],
    )


def test_update_failure_posts_final_in_same_thread():
    progress, client, say = make()
    progress.show()
    client.chat_update.side_effect = RuntimeError("failed")
    progress.send(text="answer")
    assert say.call_args.kwargs == {"text": "answer", "thread_ts": "root"}


def test_notice_failure_does_not_prevent_answer():
    progress, _, say = make()
    say.side_effect = [RuntimeError("failed"), {"ts": "final"}]
    progress.show()
    assert progress.send(text="answer") == {"ts": "final"}


def test_slow_notice_updates_existing_message_and_stops_after_completion():
    progress, client, say = make()
    progress.show()
    progress.show_slow()
    assert client.chat_update.call_count == 1
    progress.send(text="done")
    progress.show_slow()
    assert client.chat_update.call_count == 2
    assert say.call_count == 1
