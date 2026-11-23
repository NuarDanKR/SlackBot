"""채널×일 다이제스트 길잡이 (B-93 2단계 §7-4).

거는 것: 낱말이 달라도 그날 원문을 찾는다 · 다이제스트 문장은 근거로 나가지 않는다 ·
권한 밖 채널은 묻지도 않는다 · 원문이 바뀐 다이제스트는 쓰지 않는다 · 실패는 빈 목록.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from tybot import answer as answer_module
from tybot import channel_digest as cd
from tybot import digest_guide
from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.store import ArchiveStore

WS, CH, CHANNEL = "mgmt", "C0PLAN0001", "#팀-기획_abb110-사업계획"
DIGEST_BODY = "다이제스트문장-근거아님"


def _store(root: Path) -> ArchiveStore:
    writer.ingest(
        root, workspace=WS, channel=CHANNEL, channel_id=CH, acl=[CHANNEL],
        messages=[
            writer.IncomingMessage(datetime(2026, 11, 10, 1, tzinfo=UTC), "담당", "2026 경영목표 영업이익 325억"),
            writer.IncomingMessage(datetime(2026, 11, 10, 2, tzinfo=UTC), "담당", "점심 메뉴 공지"),
        ],
    )
    return ArchiveStore(root)


def _row(root: Path, *, sha=None):
    (day,) = cd.collect_channel_days(root, WS, CH)
    files = [f.to_json() for f in day.source_files]
    if sha is not None:
        files[0]["sha256"] = sha
    return {
        "workspace": WS, "channel_id": CH, "day": day.day, "terms": ["경영목표", "재무목표"],
        "source_files": files, "source_attachments": [], "score": 1, "body": DIGEST_BODY,
    }


class _Conn:
    def __init__(self, rows=None, boom=None):
        self.rows, self.boom, self.sql = rows or [], boom, []

    def cursor(self):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                conn.sql.append((sql, params))
                if conn.boom:
                    raise conn.boom

            def fetchall(self):
                return conn.rows

        return C()


CTX = RequestContext(workspace=WS, channels=frozenset({CHANNEL}))


def test_a_synonym_digest_opens_the_original_line(tmp_path):
    store = _store(tmp_path)
    hits = digest_guide.expand(store, CTX, "재무목표", conn=_Conn([_row(tmp_path)]))
    texts = [h.line.text for h in hits]
    assert any("경영목표" in t for t in texts)
    assert all(DIGEST_BODY not in t for t in texts)  # 다이제스트 문장은 근거가 아니다
    assert not any("점심" in t for t in texts)  # 낱말이 안 맞는 줄은 더하지 않는다


def test_channels_outside_the_acl_are_never_queried(tmp_path):
    store = _store(tmp_path)
    conn = _Conn([_row(tmp_path)])
    other = RequestContext(workspace=WS, channels=frozenset({"#다른채널"}))
    assert digest_guide.expand(store, other, "재무목표", conn=conn) == []
    assert conn.sql == []


def test_query_is_limited_to_visible_channels(tmp_path):
    store = _store(tmp_path)
    conn = _Conn([])
    digest_guide.expand(store, CTX, "재무목표", conn=conn)
    (sql, params), = conn.sql
    assert params["ws"] == [WS] and params["ch"] == [CH]
    assert "stale_at IS NULL" in sql


def test_a_digest_whose_original_changed_is_not_used(tmp_path):
    store = _store(tmp_path)
    assert digest_guide.expand(store, CTX, "재무목표", conn=_Conn([_row(tmp_path, sha="0" * 64)])) == []


def test_db_failure_is_an_empty_guide(tmp_path):
    store = _store(tmp_path)
    assert digest_guide.expand(store, CTX, "재무목표", conn=_Conn(boom=RuntimeError("no table"))) == []


def test_the_index_expression_matches_the_schema():
    sql = (Path(__file__).resolve().parents[1] / "deploy/sql/channel_day_digest_schema.sql").read_text(
        encoding="utf-8"
    )
    expr = (
        "lower(replace(body || ' ' || channel_day_digest_terms_text(terms), ' ', ''))"
    )
    assert expr == digest_guide.DIGEST_COMPACT_TEXT
    assert sql.count(f"(({expr})") == 2  # bigm·trgm 둘 다


def test_the_digest_index_uses_a_narrow_immutable_array_serializer():
    sql = (Path(__file__).resolve().parents[1] / "deploy/sql/channel_day_digest_schema.sql").read_text(
        encoding="utf-8"
    )
    function = sql.split(
        "CREATE OR REPLACE FUNCTION channel_day_digest_terms_text", 1
    )[1].split("COMMENT ON FUNCTION", 1)[0]
    assert "value text[]" in function
    assert "IMMUTABLE" in function
    assert "PARALLEL SAFE" in function
    index_sql = sql.split("-- 키워드 색인.", 1)[1]
    assert "array_to_string(terms" not in index_sql
    assert index_sql.count("channel_day_digest_terms_text(terms)") == 2


def test_the_engine_appends_guide_lines_after_search(monkeypatch):
    def hit(text, path, n):
        return SimpleNamespace(
            doc=SimpleNamespace(path=Path(path)),
            line=SimpleNamespace(text=text, lineno=n, source_path=None),
        )

    found = [hit("검색 줄", "a.md", 1)]
    extra = [hit("검색 줄", "a.md", 1), hit("길잡이 줄", "b.md", 2)]
    monkeypatch.setattr(digest_guide, "expand", lambda *a, **k: extra)
    engine = answer_module.AnswerEngine(SimpleNamespace(), SimpleNamespace())
    merged = engine._with_digest_guide("재무목표", found, CTX)
    assert [h.line.text for h in merged] == ["검색 줄", "길잡이 줄"]
    monkeypatch.setattr(digest_guide, "expand", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert engine._with_digest_guide("재무목표", found, CTX) == found
