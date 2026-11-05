from __future__ import annotations

from tybot import bot_access
from tybot import vipbot_runtime_access as access
from tybot.vip_access import ALLOWED, NO_SOURCES


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.cursor_value = Cursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self.cursor_value


def _run(rows, *, workspace="tyit", user="U0BQUJ60N5U"):
    connection = Connection(rows)
    decision = access.authorize_runtime(
        lambda: connection, workspace=workspace, slack_user=user
    )
    return decision, connection.cursor_value


def test_allowed_rows_become_one_exact_managed_source_set():
    rows = [
        {
            "code": ALLOWED,
            "emp_no": "E1",
            "grant_id": 7,
            "source_workspace": "mgmt",
            "channel_id": "C0AAAA111",
        },
        {
            "code": ALLOWED,
            "emp_no": "E1",
            "grant_id": 7,
            "source_workspace": "tyit",
            "channel_id": "G0BBBB222",
        },
    ]

    decision, cursor = _run(rows)

    assert decision.allowed is True
    assert decision.sources == {
        ("mgmt", "C0AAAA111"),
        ("tyit", "G0BBBB222"),
    }
    assert cursor.query == "SELECT * FROM public.vipbot_authorization(%s, %s)"
    assert cursor.params == ("tyit", "U0BQUJ60N5U")


def test_no_sources_is_not_misreported_as_no_grant():
    decision, _cursor = _run(
        [
            {
                "code": NO_SOURCES,
                "emp_no": "E1",
                "grant_id": 7,
                "source_workspace": None,
                "channel_id": None,
            }
        ]
    )

    assert decision.allowed is False
    assert decision.code == NO_SOURCES


def test_denied_rows_cannot_carry_source_coordinates():
    decision, _cursor = _run(
        [
            {
                "code": bot_access.NO_GRANT,
                "emp_no": "E1",
                "grant_id": None,
                "source_workspace": "tyit",
                "channel_id": "C0AAAA111",
            }
        ]
    )

    assert decision.code == bot_access.CHECK_FAILED


def test_mixed_decisions_or_identities_fail_closed():
    for rows in (
        [
            {"code": ALLOWED, "emp_no": "E1", "grant_id": 1},
            {"code": bot_access.NO_GRANT, "emp_no": "E1", "grant_id": 1},
        ],
        [
            {"code": ALLOWED, "emp_no": "E1", "grant_id": 1},
            {"code": ALLOWED, "emp_no": "E2", "grant_id": 1},
        ],
    ):
        decision, _cursor = _run(rows)
        assert decision.code == bot_access.CHECK_FAILED


def test_unknown_codes_and_malformed_coordinates_fail_closed():
    for row in (
        {"code": "new_code", "emp_no": "E1", "grant_id": 1},
        {
            "code": ALLOWED,
            "emp_no": "E1",
            "grant_id": 1,
            "source_workspace": "tyit",
            "channel_id": "not-a-channel",
        },
    ):
        decision, _cursor = _run([row])
        assert decision.code == bot_access.CHECK_FAILED


def test_an_allowed_row_requires_a_positive_integer_grant_id():
    for grant_id in (None, 0, -1, True, "7", 7.0):
        decision, _cursor = _run(
            [
                {
                    "code": ALLOWED,
                    "emp_no": "E1",
                    "grant_id": grant_id,
                    "source_workspace": "tyit",
                    "channel_id": "C0AAAA111",
                }
            ]
        )
        assert decision.code == bot_access.CHECK_FAILED


def test_an_unhashable_database_value_still_fails_closed():
    decision, _cursor = _run(
        [
            {
                "code": ALLOWED,
                "emp_no": "E1",
                "grant_id": {"unexpected": "value"},
                "source_workspace": "tyit",
                "channel_id": "C0AAAA111",
            }
        ]
    )

    assert decision.code == bot_access.CHECK_FAILED


def test_invalid_event_coordinates_do_not_touch_the_database():
    called = False

    def connect():
        nonlocal called
        called = True
        return Connection([])

    decision = access.authorize_runtime(
        connect, workspace="../tyit", slack_user="question body"
    )

    assert decision.code == bot_access.CHECK_FAILED
    assert called is False


def test_database_failure_is_a_denial_without_exception_text():
    def broken():
        raise RuntimeError("secret database detail")

    decision = access.authorize_runtime(
        broken, workspace="tyit", slack_user="U0BQUJ60N5U"
    )

    assert decision.code == bot_access.CHECK_FAILED
