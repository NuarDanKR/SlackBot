"""채널 공개 여부 manifest — **읽기 전용 exporter 와 그 계약.**

설계: `docs/design/hermes-write-entrypoints.md` §7.12

## 왜 이 파일이 있나

이 manifest 는 **조직 경계를 넘어 다닌다**(사내 DB → PF 파일). 그래서 두 가지가
동시에 참이어야 한다.

1. **담는 것이 다섯뿐이다** — 원문·토큰·DSN 은 물론이고 `note`·`updated_by` 같은
   운영 흔적도 안 담는다. 필요 없는 것을 담으면 그 파일이 언젠가 다른 용도로 쓰인다
2. **확인된 행만 담는다** — `is_private` 의 기본값은 `false` 다. 한 번도 동기화되지
   않은 행은 「공개」 라고 적혀 있고, 그 값을 내보내면 비공개 채널이 공개로 선언된다

둘 다 **깨져도 오류가 안 난다.** 파일은 멀쩡하고, 틀린 값으로 멀쩡히 판정된다.

DB 는 **합성**이다. 진짜 PostgreSQL 을 붙이면 이 시험은 그 환경이 있는 데서만 돌고,
안 도는 시험은 지키는 것이 없다. 여기서 재는 것은 질의와 모양이지 DB 동작이 아니다.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tybot.archive import privacy_manifest as M

ROOT = Path(__file__).resolve().parents[1]


class _FakeCursor:
    """실행한 SQL 을 그대로 들고 있는 커서. **쓰기를 받으면 터진다.**"""

    def __init__(self, rows, *, allow_readonly=True):
        self._rows = rows
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self._allow_readonly = allow_readonly

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if params is not None:
            self.params.append(params)
        if sql.strip() == "SET TRANSACTION READ ONLY" and not self._allow_readonly:
            raise RuntimeError("이 드라이버는 읽기 전용 트랜잭션을 모른다")

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows, *, allow_readonly=True):
        self.cursors: list[_FakeCursor] = []
        self._rows = rows
        self._allow_readonly = allow_readonly

    def cursor(self):
        cur = _FakeCursor(self._rows, allow_readonly=self._allow_readonly)
        self.cursors.append(cur)
        return cur


ROWS = [
    {"channel_id": "C1000FUNDS", "channel_name": "팀_자금(ABB540)_주간보고",
     "is_private": False},
    {"channel_id": "C3000PRIV0", "channel_name": "팀_인사(HRA100)_비공개",
     "is_private": True},
]


# --- ① 읽기만 한다 -----------------------------------------------------------

def test_the_exporter_only_reads():
    """① 운영 DB 에 붙는 코드다. **`SELECT` 말고는 하지 않는다.**"""
    conn = _FakeConn(ROWS)
    M.fetch(conn, "tyit")

    statements = [s.strip() for c in conn.cursors for s in c.statements]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    body = " ".join(statements[1:]).upper()
    assert body.lstrip().startswith("SELECT")
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE"):
        assert forbidden not in body, forbidden


def test_a_driver_without_read_only_transactions_still_exports():
    """② 읽기 전용 고정은 **보호막이지 전제가 아니다.**

    못 걸면 내보내기가 멈추는 것이 아니라 보호막 없이 간다 — 질의 자체가 `SELECT`
    하나뿐이라 잃는 것이 없고, 멈추면 운영이 manifest 없이 전환하려 든다.
    """
    conn = _FakeConn(ROWS, allow_readonly=False)
    assert len(M.fetch(conn, "tyit")) == 2


def test_only_verified_rows_are_exported():
    """③ `is_private` 의 기본값은 `false` 다 — **동기화 안 된 행은 「공개」 로 적혀 있다.**

    그 행을 내보내면 비공개 채널이 공개로 선언된다. `membership_checked_at` 은
    `is_private` 와 **같은 UPDATE 문**에서 채워지므로(`archiver_save_membership`),
    그 칸이 질의의 조건이어야 한다.
    """
    assert "membership_checked_at IS NOT NULL" in M.QUERY
    assert "WHERE workspace = %s" in M.QUERY


def test_the_query_is_scoped_to_one_workspace():
    """④ 워크스페이스를 안 좁히면 남의 채널 ID 가 PF 로 건너간다(원칙 4)."""
    conn = _FakeConn(ROWS)
    M.fetch(conn, "tyit")
    assert ("tyit",) in [p for c in conn.cursors for p in c.params]


# --- ② 담는 것이 다섯뿐이다 --------------------------------------------------

def test_the_manifest_carries_exactly_five_things():
    """⑤ 열이 느는 날 그 열이 조용히 PF 로 건너가면 안 된다."""
    payload = M.build("tyit", M.fetch(_FakeConn(ROWS), "tyit"),
                      now=datetime(2026, 10, 7, tzinfo=UTC))

    assert set(payload) == {"schema", "workspace", "generated_at", "channels"}
    assert payload["workspace"] == "tyit"
    assert payload["generated_at"] == "2026-10-07T00:00:00+00:00"
    for row in payload["channels"]:
        assert set(row) == set(M.ROW_KEYS), row


def test_extra_columns_do_not_leak_into_the_manifest():
    """⑥ 행을 그대로 싣지 않는다. 표에 열이 늘어도 manifest 는 그대로다."""
    noisy = [dict(ROWS[0], note="내부 메모", updated_by="U1", cutover_ts="1790000000.1")]
    payload = M.build("tyit", M.fetch(_FakeConn(noisy), "tyit"))

    raw = json.dumps(payload, ensure_ascii=False)
    for leaked in ("내부 메모", "U1", "1790000000.1", "note", "updated_by", "cutover"):
        assert leaked not in raw, leaked


def test_no_secret_shaped_text_reaches_the_manifest(tmp_path):
    """⑦ 토큰·DSN 모양의 글자가 **한 자도** 들어가면 안 된다.

    들어갈 자리가 없어 보이지만, 채널 이름은 사람이 짓는 값이라 무엇이든 들어온다.
    그래서 결과물을 직접 훑는다 — 「들어갈 리 없다」 와 「안 들어간다」 는 다르다.
    """
    path = tmp_path / "manifest.json"
    M.write(path, M.build("tyit", M.fetch(_FakeConn(ROWS), "tyit")))
    raw = path.read_text(encoding="utf-8")

    for pattern in (r"xox[baprs]-", r"postgres(?:ql)?://", r"://[^/\s]*:[^/@\s]*@",
                    r"sk-[A-Za-z0-9]", r"DATABASE_URL"):
        assert not re.search(pattern, raw), pattern


def test_the_exporter_source_never_prints_the_dsn():
    """⑧ 오류 문구는 로그로 복사되고, 복사된 로그는 저장소·메신저로 건너간다."""
    body = (ROOT / "src" / "tybot" / "archive" / "privacy_manifest.py").read_text(
        encoding="utf-8")
    # `DATABASE_URL` 을 **읽기만** 하고 값을 화면에 싣지 않는다.
    assert 'os.environ["DATABASE_URL"]' in body
    for bad in ("print(os.environ", "print(f\"{os.environ", "{exc}"):
        assert bad not in body, bad


# --- ③ 파일 쓰기 -------------------------------------------------------------

def test_the_file_is_written_atomically_and_leaves_no_debris(tmp_path):
    """⑨ 반쯤 쓰인 JSON 은 받는 쪽에서 「못 읽음」 이 되어 전환을 막는다 — 사유가 가려진다."""
    path = tmp_path / "out" / "manifest.json"
    assert M.write(path, M.build("tyit", M.fetch(_FakeConn(ROWS), "tyit"))) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == M.SCHEMA
    assert not list(path.parent.glob("*.tmp"))

    with pytest.raises((TypeError, ValueError)):
        M.write(path, {"schema": M.SCHEMA, "channels": {1, 2}})
    assert not list(path.parent.glob("*.tmp"))
    # 실패해도 **앞 판이 그대로 남는다.** 덮다 말면 받는 쪽이 전환을 못 한다.
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == M.SCHEMA


def test_a_row_without_a_channel_id_is_dropped():
    """⑩ 키가 없는 행은 대조에 쓸 수 없다. 받는 쪽에서 헷갈리느니 안 보낸다."""
    rows = [{"channel_id": "", "channel_name": "이름만", "is_private": True}, ROWS[0]]
    assert [c.channel_id for c in M.fetch(_FakeConn(rows), "tyit")] == ["C1000FUNDS"]


def test_tuple_rows_work_like_dict_rows():
    """⑪ 콘솔·잡이 자기 conn 을 넘기면 `dict_row` 가 아닐 수 있다."""
    rows = [("C1000FUNDS", "팀_자금(ABB540)_주간보고", False)]
    got = M.fetch(_FakeConn(rows), "tyit")
    assert got == [M.Channel("C1000FUNDS", "팀_자금(ABB540)_주간보고", False)]
