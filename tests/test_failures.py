"""예외 → 사용자 문구 변환.

실제 사고: LLM 키가 401 이 되자 봇이 👀 만 붙이고 답을 하지 않았다. 예외가
slack_bolt 까지 올라가 로그에만 남았고, 사용자는 봇이 무시했다고 생각했다.
"""
from __future__ import annotations

from tybot.failures import failure_message


class FakeAuthError(Exception):
    pass


def test_401_names_the_key_to_check():
    e = FakeAuthError(
        "Error code: 401 - {'type': 'error', 'error': "
        "{'type': 'authentication_error', 'message': 'API key is invalid.'}}"
    )
    msg = failure_message(e)
    assert "ANTHROPIC_API_KEY" in msg
    assert "401" in msg
    assert "check_env.py" in msg  # 점검 방법까지 알려준다


def test_rate_limit_tells_user_to_retry():
    assert "잠시" in failure_message(Exception("Error code: 429 rate_limit_error"))


def test_cost_limit_message_passes_through():
    """비용 상한은 우리가 만든 한국어 문구다 - 그대로 보여주는 게 가장 정확하다."""
    msg = failure_message(RuntimeError("일별 비용 한도(5.0 USD)를 넘어 호출을 멈췄습니다"))
    assert "한도" in msg
    assert "일별 비용" in msg


def test_slack_scope_error():
    assert "스코프" in failure_message(Exception("missing_scope: channels:history"))


def test_write_failure_names_the_paths():
    msg = failure_message(OSError("[Errno 30] Read-only file system: 'archive'"))
    assert "ARCHIVE_DIR" in msg


def test_unknown_error_still_says_something_actionable():
    msg = failure_message(ValueError("무언가 예상 못한 실패"))
    assert "오류" in msg
    assert "무언가 예상 못한 실패" in msg  # 관리자가 검색할 단서를 남긴다


def test_never_leaks_secret_values():
    """예외 문구에 키가 섞여 있어도 Slack 으로 흘리지 않는다.

    키 리터럴을 소스에 두지 않으려고 런타임에 조립한다(커밋 가드가 잡는다).
    """
    fake_key = "sk-" + "ant-api03-" + "SECRETVALUE1234567890"
    e = Exception(f"authentication_error with key {fake_key}")
    msg = failure_message(e)
    assert "SECRETVALUE" not in msg
    assert fake_key[:7] not in msg


def test_message_is_short_enough_for_slack():
    assert len(failure_message(Exception("x" * 5000))) < 500
