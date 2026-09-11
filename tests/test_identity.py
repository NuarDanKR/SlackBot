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


# --- 전체 훑기 ----------------------------------------------------------------
#
# `ensure` 는 사람이 봇을 쓸 때만 불린다. 그래서 봇을 써 볼 이유가 없던 사람은 매핑이
# 없고, 매핑이 없으면 일정 DM 같은 자동 발송에서 조용히 빠진다 — **이메일이 맞아도
# 그렇다.** 2026-09-11: 8명 팀에서 4명만 잡혀 있었고, 그 4명은 봇 명령을 써 본
# 사람들이었다.
class SweepCursor(Cursor):
    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "select slack_user from user_identity" in sql:
            self.rows = [{"slack_user": u} for u in self.conn.known]
        elif "btrim(email)" in sql:
            email = (params or {}).get("email", "")
            self.rows = self.conn.by_email.get(email, [])
        else:
            self.rows = []


class SweepConn(Conn):
    def __init__(self, *, known=(), by_email=None):
        super().__init__()
        self.known = list(known)
        self.by_email = by_email or {}

    def cursor(self):
        return SweepCursor(self)


class ListClient:
    """`users_list` 만 쓴다. 사람마다 `users_info` 를 부르면 금방 한도에 걸린다."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = 0

    def users_list(self, **kwargs):
        self.calls += 1
        members = self.pages[self.calls - 1]
        more = self.calls < len(self.pages)
        return {
            "members": members,
            "response_metadata": {"next_cursor": "next" if more else ""},
        }


def _member(uid, email="a@x.com", **over):
    row = {"id": uid, "profile": {"email": email}}
    row.update(over)
    return row


def _sweep(members, *, known=(), by_email=None, **kw):
    from tybot.identity import backfill

    conn = SweepConn(known=known, by_email=by_email or {"a@x.com": [{"emp_no": "E1"}]})
    result = backfill(conn, ListClient(members), workspace="tyit", **kw)
    return conn, result


def test_sweep_links_members_who_never_used_the_bot():
    conn, result = _sweep([_member("U1")])
    assert result.linked == 1
    assert any("insert into user_identity" in sql for sql, _ in conn.executed)


def test_sweep_skips_people_already_linked():
    conn, result = _sweep([_member("U1")], known=["U1"])
    assert (result.linked, result.already) == (0, 1)
    # 이미 이어진 사람 때문에 쓰기를 하지 않는다.
    assert not any("insert into user_identity" in sql for sql, _ in conn.executed)


def test_sweep_skips_bots_and_deleted_accounts():
    _, result = _sweep([
        _member("U1", is_bot=True),
        _member("U2", deleted=True),
        _member("USLACKBOT"),
    ])
    assert (result.linked, result.skipped) == (0, 3)


def test_sweep_reports_who_could_not_be_linked():
    """조치는 사람이 한다 — 프로필 이메일과 인사 이메일이 다른 경우다."""
    _, result = _sweep([
        _member("U1", email=""),
        _member("U2", email="unknown@x.com"),
    ])
    assert (result.no_email, result.no_match) == (1, 1)
    assert result.unmatched == ["U1", "U2"]


def test_sweep_keeps_no_email_in_the_report():
    """이메일은 사번을 찾는 데만 쓰고 버린다. 목록에 담으면 복제된다."""
    _, result = _sweep([_member("U1", email="someone@taeyoung.com")],
                       by_email={})
    assert result.unmatched == ["U1"]
    assert all("@" not in u for u in result.unmatched)


def test_sweep_follows_pagination():
    from tybot.identity import backfill

    conn = SweepConn(by_email={"a@x.com": [{"emp_no": "E1"}]})
    client = ListClient([_member("U1")], [_member("U2")])
    result = backfill(conn, client, workspace="tyit")
    assert client.calls == 2
    assert result.linked == 2


def test_sweep_ambiguous_email_is_not_linked():
    """같은 이메일에 직원이 둘이면 어느 쪽인지 모른다. 찍지 않는다."""
    _, result = _sweep([_member("U1")],
                       by_email={"a@x.com": [{"emp_no": "E1"}, {"emp_no": "E2"}]})
    assert result.linked == 0
    assert result.no_match == 1


def test_dry_run_writes_nothing():
    conn, result = _sweep([_member("U1")], dry_run=True)
    assert result.linked == 1  # 무엇이 바뀔지는 센다
    assert not any("insert into user_identity" in sql for sql, _ in conn.executed)


def test_sweep_uses_the_same_matching_sql_as_ensure():
    """규칙이 갈라지면 명령으로는 이어지고 훑기로는 안 이어지는 사람이 생긴다."""
    import inspect

    from tybot.identity import backfill

    src = inspect.getsource(backfill)
    assert "EMPLOYEE_BY_EMAIL_SQL" in src
    assert "UPSERT_SQL" in src
