"""App Token 묶음 조사 — **몇 개의 Socket 을 열어야 하나.**

결정: 2026-09-29 supervisor 작업지시서 §2.1 · §7-1.

Slack 은 같은 앱의 Socket 연결이 여러 개면 payload 를 어느 쪽으로 보낼지 보장하지
않는다. 그래서 묶음을 잘못 세면 **이벤트가 조용히 한쪽으로만 간다** — 오류가 아니라
누락이다.

그리고 이 조사는 토큰을 만진다. 그래서 두 번째 계약이 있다: **값이 밖으로 나가지
않는다.** 반환값·요약·표시 어디에도 토큰이 없고, 묶음 표시는 실행마다 바뀐다.
"""

from __future__ import annotations

import re
from pathlib import Path

from tybot.archive import token_topology as topology
from tybot.archive.token_topology import Connection

ROOT = Path(__file__).resolve().parent.parent

APP_A = "xapp-" + "a" * 30
APP_B = "xapp-" + "b" * 30
BOT_1 = "xoxb-" + "1" * 30
BOT_2 = "xoxb-" + "2" * 30


def _conn(workspace: str, *, app: str = APP_A, bot: str = BOT_1) -> Connection:
    return Connection(workspace=workspace, bot_key="archiver", bot_token=bot, app_token=app)


# --- 몇 개를 열어야 하나 --------------------------------------------------------

def test_the_same_app_token_shares_one_socket():
    """**이 시험이 이 모듈의 이유다.**

    같은 앱을 두 워크스페이스에 설치했으면 Socket 은 하나다. 둘을 열면 Slack 이
    이벤트를 아무 쪽에나 보내고, 받지 못한 쪽 워크스페이스는 조용히 빈다.
    """
    result = topology.survey([_conn("tyit", bot=BOT_1), _conn("mgmt", bot=BOT_2)])

    assert result.socket_count == 1
    assert result.groups[0].workspaces == ["mgmt", "tyit"]


def test_different_app_tokens_need_their_own_socket():
    result = topology.survey([
        _conn("tyit", app=APP_A, bot=BOT_1),
        _conn("pf", app=APP_B, bot=BOT_2),
    ])

    assert result.socket_count == 2
    assert [group.workspaces for group in result.groups] == [["pf"], ["tyit"]] or \
           [group.workspaces for group in result.groups] == [["tyit"], ["pf"]]


def test_a_connection_without_an_app_token_cannot_open_a_socket():
    """App Token 이 없으면 Socket 이 안 열린다. 「연결됨」 으로 세면 안 된다."""
    result = topology.survey([_conn("tyit"), Connection("mgmt", "archiver", BOT_2, "")])

    assert result.socket_count == 1
    assert result.without_app_token == ["mgmt"]
    assert any(group.missing_app_token for group in result.groups)


def test_no_connections_is_not_a_socket():
    assert topology.survey([]).socket_count == 0


# --- 설치 실수 -------------------------------------------------------------------

def test_the_same_bot_token_in_two_workspaces_is_reported():
    """한 워크스페이스 토큰이 다른 연결에 들어가 있으면 **남의 대화를 수집한다.**

    오류가 아니라 유출이므로 Socket 묶음과 따로 센다.
    """
    result = topology.survey([
        _conn("tyit", app=APP_A, bot=BOT_1),
        _conn("mgmt", app=APP_A, bot=BOT_1),
    ])

    assert result.shared_bot_tokens == [["mgmt", "tyit"]]


def test_shared_bot_token_is_reported_even_when_app_token_is_missing():
    """App Token 누락이 더 위험한 Bot Token 오설정을 가리면 안 된다."""
    result = topology.survey([
        _conn("tyit", app="", bot=BOT_1),
        _conn("mgmt", app=APP_A, bot=BOT_1),
    ])

    assert result.without_app_token == ["tyit"]
    assert result.shared_bot_tokens == [["mgmt", "tyit"]]


def test_distinct_bot_tokens_are_not_reported():
    result = topology.survey([
        _conn("tyit", app=APP_A, bot=BOT_1),
        _conn("mgmt", app=APP_A, bot=BOT_2),
    ])

    assert result.shared_bot_tokens == []


# --- 값이 나가지 않는다 ----------------------------------------------------------

def test_the_summary_contains_no_token():
    result = topology.survey([
        _conn("tyit", app=APP_A, bot=BOT_1),
        _conn("mgmt", app=APP_A, bot=BOT_1),
    ])

    text = "\n".join(topology.summarize(result))

    assert APP_A not in text and BOT_1 not in text
    assert "xoxb-" not in text and "xapp-" not in text


def test_the_summary_contains_no_fingerprint():
    """표시가 화면에 나오면 사람이 그걸 옮겨 적고, 옮겨 적힌 것은 남는다."""
    result = topology.survey([_conn("tyit")])

    text = "\n".join(topology.summarize(result))
    mark = topology.fingerprint(APP_A)

    assert mark not in text
    assert not re.search(r"\b[0-9a-f]{16}\b", text)


def test_the_result_object_never_carries_a_token():
    result = topology.survey([_conn("tyit", app=APP_A, bot=BOT_1)])

    assert APP_A not in repr(result) and BOT_1 not in repr(result)


def test_a_fingerprint_cannot_be_matched_across_runs():
    """**실행마다 소금이 바뀐다.** 저장할 수 있으면 언젠가 저장되고, 저장된 것은 샌다.

    소금을 바꿔치기해서 보지 않는다 — 그러면 소금이 상수로 박혀 있어도 통과한다.
    **다른 프로세스**에서 같은 토큰을 세어 값이 다른지 본다.
    """
    import subprocess
    import sys

    other = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'src');"
         "from tybot.archive.token_topology import fingerprint;"
         f"print(fingerprint({APP_A!r}))"],
        capture_output=True, text=True, cwd=str(ROOT), check=True,
    ).stdout.strip()

    assert other, "다른 프로세스가 표시를 못 만들었다"
    assert other != topology.fingerprint(APP_A)


def test_an_empty_token_has_no_fingerprint():
    """빈 값이 같은 표시를 받으면 **토큰 없는 연결끼리 한 Socket 으로 묶인다.**"""
    assert topology.fingerprint("") == ""


# --- 요약 문구 -------------------------------------------------------------------

def test_the_summary_says_how_many_sockets():
    result = topology.survey([_conn("tyit"), _conn("mgmt")])

    assert "열어야 하는 Socket 연결: 1개" in "\n".join(topology.summarize(result))


def test_the_summary_explains_the_shared_bot_token():
    result = topology.survey([_conn("tyit", bot=BOT_1), _conn("mgmt", bot=BOT_1)])

    text = "\n".join(topology.summarize(result))

    assert "설치를 확인하세요" in text
    assert "남의 대화를 수집합니다" in text
