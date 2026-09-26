"""첨부 정본 재색인 — **무엇을 넣고, 무엇을 지우고, 두 번째 실행이 0 인가.**

결정: 2026-09-26 오너 지시 5·6번.

DB 를 요구하지 않는다. `raw_line` 을 집합 하나로 흉내 내고 스크립트의 판정만 본다 —
확인하려는 것은 SQL 이 아니라 **무엇을 지우기로 정하는가** 다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tybot.archive import attachment_doc
from tybot.archive.store import ArchiveStore

ROOT = Path(__file__).resolve().parent.parent
WS, CH, CHANNEL = "tyit", "C0FUND", "#팀_자금(ABB540)_주간보고"


def _script():
    path = ROOT / "scripts" / "reindex_attachments.py"
    spec = importlib.util.spec_from_file_location("reindex_attachments", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reindex_attachments"] = module
    spec.loader.exec_module(module)
    return module


script = _script()


def _doc(**over) -> attachment_doc.AttachmentDoc:
    base = {
        "workspace": WS, "channel_id": CH, "channel": CHANNEL,
        "file_id": "F1", "name": "기성내역.xlsx", "revision": "aaaa11112222",
        "visibility": "private", "acl": frozenset({CHANNEL}),
        "text": "9월 기성 청구액은 15억입니다", "conversion_state": attachment_doc.CONVERTED,
        "message_ts": "1790070000.000001", "filetype": "xlsx", "sha256": "a" * 64,
        "staged_at": "2026-09-22T10:00:00+00:00",
    }
    return attachment_doc.AttachmentDoc(**(base | over))


def _write(root: Path, doc: attachment_doc.AttachmentDoc) -> Path:
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(attachment_doc.render(doc), encoding="utf-8")
    return path


def _channel_raw(root: Path) -> None:
    from datetime import UTC, datetime

    from tybot.archive import writer

    writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CH,
        messages=[writer.IncomingMessage(datetime.now(UTC), "김자금", "사람 발언")],
        acl=[CHANNEL],
    )


class FakeIndex:
    """`raw_line` 을 좌표 집합으로. 넣기는 멱등, 지우기는 **있는 것만** 센다."""

    def __init__(self) -> None:
        self.rows: set[tuple[str, int, str]] = set()

    def fetch(self, paths: list[str]) -> set[tuple[str, int, str]]:
        wanted = set(paths)
        return {row for row in self.rows if row[0] in wanted}

    def insert(self, keep, root) -> int:
        before = len(self.rows)
        self.rows |= script.expected_keys(keep, root)
        return len(self.rows) - before

    def delete(self, keys) -> int:
        keys = set(keys)
        hit = self.rows & keys
        self.rows -= hit
        return len(hit)


def run(store: ArchiveStore, index: FakeIndex) -> tuple[int, int]:
    """한 번 돌린 결과 `(새로 넣은 줄, 실제로 지운 행)`."""
    keep, stale = script.plan(store, index.fetch)
    return index.insert(keep, store.root), index.delete(stale)


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "archive"
    _channel_raw(root)
    return root


# --- 무엇을 넣나 ----------------------------------------------------------------

def test_only_the_current_revision_is_indexed(archive):
    """스크립트가 판정을 다시 쓰지 않는다 — reader 가 고른 것만 넣는다."""
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc(revision="new000000000", text="새 판",
                         staged_at="2026-09-22T10:00:00+00:00"))

    keep, _ = script.plan(ArchiveStore(archive), lambda paths: set())

    assert [str(doc.path).endswith("new000000000.md") for doc in keep] == [True]


def test_nothing_to_remove_when_the_index_is_empty(archive):
    """지울 것은 **DB 에 있는 것** 중에서만 나온다. 없는 행을 세면 멱등성을 못 본다."""
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _write(archive, _doc())

    _, stale = script.plan(ArchiveStore(archive), lambda paths: set())

    assert stale == []


# --- 무엇을 빼나 ----------------------------------------------------------------

def test_an_old_revision_left_in_the_index_is_removed(archive):
    """옛 판이 남으면 히트 수가 부풀고 직접 조회와 색인의 후보가 갈린다."""
    index = FakeIndex()
    old = _write(archive, _doc(revision="old000000000", text="8월 기성 12억",
                               staged_at="2026-08-01T10:00:00+00:00"))
    run(ArchiveStore(archive), index)
    assert any("old000000000" in row[0] for row in index.rows)

    _write(archive, _doc(revision="new000000000", text="9월 기성 15억",
                         staged_at="2026-09-22T10:00:00+00:00"))
    _, removed = run(ArchiveStore(archive), index)

    assert removed > 0
    assert not any("old000000000" in row[0] for row in index.rows), old
    assert any("new000000000" in row[0] for row in index.rows)


def test_an_old_content_hash_at_the_current_path_is_removed(archive):
    """**같은 경로에 남은 옛 해시도 지운다.**

    파일 기준으로만 정리하면 이건 못 잡는다 — 경로가 「넣을 것」 이라 통째로
    건너뛴다. 그러면 옛 본문이 계속 검색 후보로 올라온다.
    """
    index = FakeIndex()
    _write(archive, _doc(text="9월 기성 12억"))
    run(ArchiveStore(archive), index)
    stale_rows = set(index.rows)

    # 같은 revision 경로가 다른 내용으로 다시 쓰인 적이 있는 상태.
    _write(archive, _doc(text="9월 기성 15억"))
    _, removed = run(ArchiveStore(archive), index)

    assert removed == len(stale_rows)
    assert not (index.rows & stale_rows)
    assert index.rows, "새 본문은 남아 있어야 한다"


def test_the_second_run_changes_nothing(archive):
    """멱등성. 두 번째 실행의 **실제 변경 건수가 0** 이어야 한다."""
    index = FakeIndex()
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    run(ArchiveStore(archive), index)
    _write(archive, _doc(revision="new000000000", text="새 판",
                         staged_at="2026-09-22T10:00:00+00:00"))
    run(ArchiveStore(archive), index)
    settled = set(index.rows)

    inserted, removed = run(ArchiveStore(archive), index)

    assert (inserted, removed) == (0, 0)
    assert index.rows == settled


# --- 어느 아카이브를 넣나 --------------------------------------------------------

def test_the_configured_archive_needs_no_isolated_index(tmp_path):
    configured = str(tmp_path / "archive")

    assert script.archive_refusal(configured, configured, "") == ""
    assert script.archive_refusal(None, configured, "") == ""


def test_another_archive_is_refused_without_an_isolated_index(tmp_path):
    """남의 아카이브를 운영 색인에 부으면 권한 밖 본문이 검색 후보가 된다."""
    refusal = script.archive_refusal(
        str(tmp_path / "pf-archive"), str(tmp_path / "archive"), "",
    )

    # 「이름을 못 읽었다」 같은 **다른 사유로 우연히 거절되면 안 된다.** 거절 사유가
    # 바뀌면 사람이 --index-dsn 을 붙여 같은 실수를 다시 한다.
    assert "설정된 운영 아카이브가 아닙니다" in refusal
    assert "--index-dsn" in refusal


def test_another_archive_is_refused_into_an_operational_looking_index(tmp_path, monkeypatch):
    """격리 DSN 을 줬다고 다 되는 것이 아니다 — 이름이 운영·실측 DB 면 거부한다."""
    monkeypatch.setattr(script, "_isolation", _fake_isolation)

    refusal = script.archive_refusal(
        str(tmp_path / "pf"), str(tmp_path / "archive"),
        "postgresql://u@localhost/tybot_archive_bench",
    )

    # 이름이 사유에 들어가므로 「bench 가 들어 있다」 만으로는 **어떤 검사가
    # 걸렀는지 모른다.** 실측 DB 라서 막았다는 것을 본다.
    assert "다른 작업이 쓰는 DB" in refusal


def test_the_configured_operational_database_is_never_an_isolated_index(tmp_path, monkeypatch):
    """설정 파일이 가리키는 DB 는 **이름이 어떻든** 격리 색인이 아니다.

    환경변수로 올라와 있지 않아도 운영 대상이다(2026-09-25 개발 PC 가 그 상태였다).
    """
    monkeypatch.setattr(script, "_isolation", _fake_isolation)

    refusal = script.archive_refusal(
        str(tmp_path / "pf"), str(tmp_path / "archive"),
        "postgresql://u@localhost/tybot_main",
    )

    assert "설정 파일이 가리키는 운영 DB" in refusal


def test_a_dsn_that_does_not_look_isolated_is_refused(tmp_path, monkeypatch):
    """격리 DB 인지 **이름으로 알 수 없으면** 돌리지 않는다. 막는 쪽이 기본값이다."""
    monkeypatch.setattr(script, "_isolation", _fake_isolation)

    refusal = script.archive_refusal(
        str(tmp_path / "pf"), str(tmp_path / "archive"),
        "postgresql://u@localhost/tybot_tmp",
    )

    assert "이름으로 알 수 없습니다" in refusal


def test_another_archive_into_a_scratch_index_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "_isolation", _fake_isolation)

    refusal = script.archive_refusal(
        str(tmp_path / "pf"), str(tmp_path / "archive"),
        "postgresql://u@localhost/tybot_scratch",
    )

    assert refusal == ""


#: 실제 검증 스크립트. 표시 목록은 여기서 가져온다(복사하지 않는다).
REAL_GUARD = script._isolation()


def _fake_isolation():
    """표시 목록은 진짜를 쓰고, **설정 파일만 안 읽는다.**

    개발 PC 의 `.env` 를 읽으면 시험 결과가 그 PC 설정에 따라 달라진다.
    """
    real = REAL_GUARD

    class Guard:
        dsn_database = staticmethod(real.dsn_database)
        RESERVED_DB_MARKERS = real.RESERVED_DB_MARKERS
        SAFE_DB_MARKERS = real.SAFE_DB_MARKERS

        @staticmethod
        def configured_databases() -> set[str]:
            return {"tyslackai", "tybot_main"}

    return Guard()


def test_the_isolation_markers_come_from_the_verifier():
    """표시 목록을 복사하면 한쪽만 고치는 날이 온다."""
    guard = script._isolation()

    assert "tyslackai" in guard.RESERVED_DB_MARKERS
    assert guard.SAFE_DB_MARKERS


# --- 시작 전에 멈추는가 ----------------------------------------------------------

def test_the_script_refuses_without_a_database(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["reindex_attachments.py", "--dry-run"])
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda: "")

    assert script.main() == 2
    assert "DATABASE_URL" in capsys.readouterr().out


def test_the_script_refuses_while_broken_documents_remain(archive, monkeypatch, capsys):
    """권한 칸이 깨진 정본을 색인하면 「없는 자료」 로 굳는다. DB 를 열기 전에 멈춘다."""
    path = archive / _doc().relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        attachment_doc.render(_doc()).replace(f"acl: [{CHANNEL}]", "acl:"),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda: "")
    # 색인을 **열어 보기도 전에** 멈춰야 한다. 열면 여기서 터진다.
    monkeypatch.setattr(script, "fetch_existing", _never_called)
    monkeypatch.setattr("tybot.paths.archive_dir", lambda: str(archive))
    monkeypatch.setattr(sys, "argv", ["reindex_attachments.py", "--dry-run"])

    assert script.main() == 3
    out = capsys.readouterr().out
    assert "근거로 못 쓰는 정본 1건" in out
    assert "repair_attachment_rights.py" in out, "고칠 수 있는 건 가는 길을 알려야 한다"


def _never_called(paths):
    raise AssertionError(f"색인을 열면 안 된다: {len(paths)}개 경로")


# --- 진짜 DB 모양으로 ------------------------------------------------------------
#
# 2026-09-26 오너 QA 1·5번. 여기서 확인하는 것은 판정이 아니라 **드라이버 계약**이다.
#
# - `workspace_store._connect` 는 `dict_row` 다. 행을 `row[0]` 으로 읽으면 KeyError 고,
#   그러면 정리가 안 되는 게 아니라 스크립트가 터진다
# - `ON CONFLICT DO NOTHING` 으로 건너뛴 행을 「넣었다」 로 세면 두 번째 실행도
#   첫 번째와 같은 건수를 보고한다 — 멱등한지 출력으로는 알 수 없다


class FakeCursor:
    """`raw_line` 한 표. **psycopg 가 하는 대로** 돌려준다."""

    def __init__(self, rows: dict):
        self._rows = rows
        self._result: list[dict] = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        assert "SELECT doc_path" in sql, sql
        wanted = set(params[0])
        self._result = [
            {"doc_path": key[0], "line_no": key[1], "content_sha": key[2]}
            for key in sorted(self._rows)
            if key[0] in wanted
        ]
        self.rowcount = len(self._result)

    def executemany(self, sql, rows):
        if "INSERT INTO raw_line" in sql:
            new = 0
            for row in rows:
                key = (row[2], row[3], row[7])
                if key not in self._rows:
                    self._rows[key] = row
                    new += 1
            self.rowcount = new  # ON CONFLICT 로 건너뛴 행은 안 센다
            return
        assert "DELETE FROM raw_line" in sql, sql
        gone = 0
        for key in rows:
            if tuple(key) in self._rows:
                del self._rows[tuple(key)]
                gone += 1
        self.rowcount = gone

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self, rows: dict):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return FakeCursor(self._rows)


def _run_main(monkeypatch, archive, rows) -> str:
    from tybot import search_index
    from tybot.console import workspace_store

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/none")
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda: "")
    monkeypatch.setattr("tybot.paths.archive_dir", lambda: str(archive))
    monkeypatch.setattr(search_index, "_connect", lambda: FakeConn(rows))
    monkeypatch.setattr(workspace_store, "_connect", lambda: FakeConn(rows))
    monkeypatch.setattr(sys, "argv", ["reindex_attachments.py"])

    assert script.main() == 0


def test_rows_are_read_by_name_not_by_position(archive, monkeypatch, capsys):
    """`dict_row` 연결에서 `row[0]` 은 KeyError 다. 터지면 아무것도 안 지워진다."""
    rows: dict = {}
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _run_main(monkeypatch, archive, rows)
    capsys.readouterr()

    _write(archive, _doc(revision="new000000000", text="새 판",
                         staged_at="2026-09-22T10:00:00+00:00"))
    _run_main(monkeypatch, archive, rows)

    out = capsys.readouterr().out
    assert "뺀 과거 색인 행: 0개" not in out, "옛 판을 찾아 지웠어야 한다"
    assert not any("old000000000" in key[0] for key in rows)


def test_the_second_run_reports_zero_inserted_and_zero_removed(archive, monkeypatch, capsys):
    """멱등성은 **출력으로 확인할 수 있어야** 한다."""
    rows: dict = {}
    _write(archive, _doc(revision="old000000000", text="옛 판",
                         staged_at="2026-08-01T10:00:00+00:00"))
    _run_main(monkeypatch, archive, rows)
    _write(archive, _doc(revision="new000000000", text="새 판",
                         staged_at="2026-09-22T10:00:00+00:00"))
    _run_main(monkeypatch, archive, rows)
    settled = dict(rows)
    capsys.readouterr()

    _run_main(monkeypatch, archive, rows)

    out = capsys.readouterr().out
    assert "새로 색인한 줄: 0개" in out
    assert "뺀 과거 색인 행: 0개" in out
    assert rows == settled


def test_a_broken_document_stops_the_run_before_the_index(archive, monkeypatch, capsys):
    """reader 가 거절하는 문서를 색인하면 「없는 자료」 로 굳는다."""
    rows: dict = {}
    _write(archive, _doc(conversion_state="누가봐도이상한상태"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/none")
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda: "")
    monkeypatch.setattr("tybot.paths.archive_dir", lambda: str(archive))
    monkeypatch.setattr(script, "fetch_existing", _never_called)
    monkeypatch.setattr(sys, "argv", ["reindex_attachments.py", "--dry-run"])

    assert script.main() == 3
    assert "unknown_state" in capsys.readouterr().out
    assert rows == {}
