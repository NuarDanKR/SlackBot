from __future__ import annotations

from tybot.identity import ensure


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "from user_identity" in sql:
            self.rows = self.conn.identity
        elif "btrim(email)" in sql:
            self.rows = self.conn.employee
        else:
            self.rows = []

    def fetchall(self):
        return self.rows


class Conn:
    autocommit = True

    def __init__(self, *, identity=(), employee=()):
        self.identity = list(identity)
        self.employee = list(employee)
        self.executed = []

    def cursor(self):
        return Cursor(self)


class Client:
    def __init__(self, email="member@example.com"):
        self.email = email
        self.calls = 0

    def users_info(self, **kwargs):
        self.calls += 1
        return {"user": {"profile": {"email": self.email}}}


def test_existing_identity_does_not_read_slack_email():
    conn = Conn(identity=[{"emp_no": "E1"}])
    client = Client()
    assert ensure(conn, client, workspace="tyit", slack_user="U1") == "E1"
    assert client.calls == 0


def test_email_match_creates_verified_identity_without_storing_email():
    conn = Conn(employee=[{"emp_no": "E1"}])
    assert ensure(conn, Client(), workspace="tyit", slack_user="U1") == "E1"
    sql, params = conn.executed[-1]
    assert "verified_by" in sql and "email_match" in sql
    assert params == {"workspace": "tyit", "slack_user": "U1", "emp_no": "E1"}
    assert all("email" not in params for sql, params in conn.executed if params and "insert" in sql)


def test_unmatched_email_does_not_create_an_identity():
    conn = Conn(employee=[])
    assert ensure(conn, Client(), workspace="tyit", slack_user="U1") is None
    assert not any("insert into user_identity" in sql for sql, _ in conn.executed)


def test_manual_mapping_cannot_be_overwritten_by_email_match():
    from tybot.identity import UPSERT_SQL

    assert "user_identity.verified_by = 'email_match'" in UPSERT_SQL
