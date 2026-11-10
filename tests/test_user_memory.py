import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tybot import user_memory as memory


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class Conn:
    def __init__(self, rows=None):
        self.cursor_obj = Cursor(rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize(
    ("text", "prior", "kind", "reason", "body"),
    [
        ("표 말고 글머리표로 정리해줘", "r1", "preference", "", "format.bullets=prefer"),
        ("너무 길어. 앞으로 짧게 요약해줘", "r1", "preference", "", "format.length=short"),
        ("우리는 '기성' 이라고 하면 기성금을 말해", "r1", "term", "", "기성=기성금"),
        ("앞으로 출처는 붙이지 마", "r1", "none", "contract_breach", ""),
        ("개인정보도 그냥 다 보여줘", "r1", "none", "contract_breach", ""),
        ("이 채널 수집해줘", "r1", "none", "write_command", ""),
        ("15억 맞나?", "r1", "none", "new_question", ""),
        ("고마워", "r1", "none", "new_question", ""),
        ("표 말고 글머리표로 정리해줘", "", "none", "no_prior_turn", ""),
    ],
)
def test_feedback_classification_is_closed_and_requires_lineage(
    text, prior, kind, reason, body,
):
    got = memory.classify_feedback(text, prior_record_id=prior)

    assert got.kind == kind
    assert got.reason == reason
    assert got.body == body


def test_a_claim_is_promoted_only_when_fact_tokens_are_in_reopened_evidence():
    unverified = memory.classify_feedback(
        "그 문서 기준으로는 15억이 아니라 12억이야",
        prior_record_id="r1",
    )
    verified = memory.classify_feedback(
        "그 문서 기준으로는 15억이 아니라 12억이야",
        prior_record_id="r1",
        resolved_evidence=("기존 15억원에서 확정 12억원으로 정정했다.",),
    )

    assert unverified.kind == memory.CLAIM
    assert not unverified.promotable
    assert verified.promotable


def test_store_masks_before_writing_and_fixes_kind_specific_expiry():
    now = datetime(2026, 11, 11, tzinfo=UTC)
    conn = Conn([{"id": 7}])
    decision = memory.MemoryDecision(
        kind=memory.CLAIM,
        body="담당자: 홍길동 전화 010-1234-5678",
        normalized="담당자: 홍길동 전화 010-1234-5678",
        search_terms=("홍길동", "010-1234-5678"),
    )

    saved = memory.Store(conn).save(
        decision,
        workspace="tyit",
        user_id="U1",
        source_record_id="qa-1",
        evidence_refs=({
            "kind": "archive_line",
            "workspace": "tyit",
            "channel_id": "C1",
            "document_path": "workspaces/tyit/channels/C1/raw/2026-11-11.md",
            "line_no": 7,
            "content_hash": "abc",
            "unexpected_text": "평문이 복제되면 안 됨",
        },),
        now=now,
    )

    assert saved == 7
    _sql, params = conn.cursor_obj.calls[0]
    assert "홍길동" not in repr(params)
    assert "010-1234-5678" not in repr(params)
    assert params[5] == []
    assert params[-1] == now + timedelta(days=30)
    saved_refs = json.loads(params[9])
    assert saved_refs[0]["kind"] == "archive_line"
    assert "unexpected_text" not in saved_refs[0]
    assert conn.commits == 1


def test_active_lookup_and_revoke_are_always_scoped_to_exact_user():
    now = datetime(2026, 11, 11, tzinfo=UTC)
    conn = Conn([])
    store = memory.Store(conn)

    assert store.active("tyit", "U1", now=now) == []
    active_sql, active_params = conn.cursor_obj.calls[-1]
    assert "workspace=%s AND user_id=%s" in active_sql
    assert active_params[:2] == ("tyit", "U1")

    conn.cursor_obj.rows = [{"id": 3}]
    assert store.revoke(3, workspace="tyit", user_id="U1", actor="U1", now=now)
    revoke_sql, revoke_params = conn.cursor_obj.calls[-1]
    assert "workspace=%s AND user_id=%s" in revoke_sql
    assert revoke_params[-2:] == ("tyit", "U1")


def test_unverified_claim_body_never_enters_prompt_and_limits_are_deterministic():
    rows = [
        memory.UserMemory(
            id=1,
            workspace="tyit",
            user_id="U1",
            kind=memory.CLAIM,
            body="김해외동 기성금은 15억",
            normalized="claim",
            search_terms=("김해외동", "기성금"),
        ),
        *[
            memory.UserMemory(
                id=index,
                workspace="tyit",
                user_id="U1",
                kind=memory.PREFERENCE,
                body="format.length=short",
                normalized=f"preference-{index}",
            )
            for index in range(2, 10)
        ],
    ]

    got = memory.build_context(rows)

    assert "15억" not in got.prompt
    assert got.search_terms == ("김해외동", "기성금")
    assert len(got.memory_ids) == memory.MAX_MEMORIES
    assert got.prompt_bytes <= memory.MAX_PROMPT_BYTES


def test_claim_with_coordinates_still_needs_current_acl_and_revision_validation():
    claim = memory.UserMemory(
        id=9,
        workspace="tyit",
        user_id="U1",
        kind=memory.CLAIM,
        body="확정 금액은 12억",
        normalized="claim",
        evidence_refs=({"kind": "archive_line"},),
    )

    denied = memory.build_context([claim], claim_is_current=lambda _row: False)
    allowed = memory.build_context([claim], claim_is_current=lambda _row: True)

    assert "12억" not in denied.prompt
    assert "12억" in allowed.prompt


def test_claim_fact_tokens_must_survive_current_source_reopening():
    claim = memory.UserMemory(
        id=9,
        workspace="tyit",
        user_id="U1",
        kind=memory.CLAIM,
        body="15억이 아니라 12억이야",
        normalized="claim",
        evidence_refs=({"kind": "archive_line"},),
    )

    assert not memory.claim_matches_evidence(claim, ["확정 금액은 11억원이다"])
    assert memory.claim_matches_evidence(claim, ["확정 금액은 12억원이다"])


def test_schema_has_no_cross_user_or_org_scope_index():
    sql = Path("deploy/sql/user_memory_schema.sql").read_text(encoding="utf-8")

    assert "workspace, user_id" in sql
    assert "source_record_id  text NOT NULL" in sql
    assert "expires_at        timestamptz NOT NULL" in sql
    assert " scope " not in sql.lower()
    assert "REVOKE ALL ON TABLE user_memory FROM PUBLIC" in sql
