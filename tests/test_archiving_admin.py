"""콘솔 운영 손잡이 — **전이 함수와 게이트를 우회하지 못한다.**

결정: 2026-09-25 오너 §5·§6·§9·§10.

여기서 막는 것 셋.

1. **`mode` 와 `writer_owner` 를 따로 갱신하는 것.** 따로 바꾸면 「active 인데
   주인은 master」 가 만들어지고, 그 상태에서 두 writer 가 같은 파일에 쓴다.
   줄이 섞이고 `doc_count` 가 유실되는데 아무도 예외를 안 받는다
2. **사람과 사유 없이 바꾸는 것.** 없으면 사고가 났을 때 범위를 정할 수 없다
3. **검증 안 된 스키마로 `active` 에 가는 것.** DBA 권한이 없어 격리 DB 검증을
   못 하는 동안, 「했다고 기억하는」 것을 막는다

가짜 저장소를 쓴다. 커서를 흉내 내면 시험이 규칙이 아니라 SQL 문장 모양을
지키게 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_archiving_repo import FakeArchivingRepo

from tybot.archive.archiving_state import ChannelMode, WriterOwner
from tybot.console import archiving_admin as admin
from tybot.console import release_gate

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src" / "tybot" / "console" / "archiving_admin.py").read_text(
    encoding="utf-8"
)


@pytest.fixture
def repo() -> FakeArchivingRepo:
    return FakeArchivingRepo()


@pytest.fixture
def gate_open(monkeypatch, tmp_path):
    """검증을 통과한 상태를 만든다. **지문까지 맞춘다.**"""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    release_gate.record_pass(by="dba", dsn_label="tybot_schema_test")
    return tmp_path


@pytest.fixture
def gate_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    return tmp_path


def _ready_for_active(repo: FakeArchivingRepo) -> None:
    repo.given_retention("bot_conversation_audit", 90)
    repo.given_retention("bot_dm_attachment", 30)
    for name in (
        "archiver_writes_live",
        "preserve_edit_delete",
        "require_attachment_ack",
        "revision_reader_ready",
    ):
        repo.given_flag(name, True)


# --- 사람과 사유 없이는 못 바꾼다 --------------------------------------------

def test_an_actor_without_a_name_is_refused():
    with pytest.raises(admin.AdminRefused, match="바꾼 사람"):
        admin.Actor("", "파일럿 시작")


def test_an_actor_without_a_reason_is_refused():
    """기본값을 두면 전부 그 기본값으로 남고, 그건 기록이 아니다."""
    with pytest.raises(admin.AdminRefused, match="사유"):
        admin.Actor("dan", "   ")


def test_every_write_records_the_actor_and_the_reason(repo, gate_closed):
    admin.set_channel_mode(
        "tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "파일럿"), repo=repo
    )

    assert len(repo.audit_rows) == 1
    assert repo.audit_rows[0]["actor"] == "dan"
    assert repo.audit_rows[0]["reason"] == "파일럿"


# --- 검증 게이트 -------------------------------------------------------------

def test_active_is_blocked_until_the_schema_is_verified(repo, gate_closed):
    """DBA 권한이 없어 검증을 못 하는 동안 **운영 주인이 바뀌지 않는다.**"""
    repo.given_channel("tyit", "C1", "shadow")

    with pytest.raises(admin.AdminRefused) as caught:
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "인수"),
            cutover_ts="1700000000.0001", repo=repo,
        )

    assert "active 전환" in str(caught.value)
    assert "검증" in str(caught.value)
    assert repo.channel_rows[("tyit", "C1")]["mode"] == "shadow", "아무것도 안 바뀐다"
    assert repo.audit_rows == []


def test_shadow_is_not_blocked_by_the_gate(repo, gate_closed):
    """그림자는 운영 원문을 안 건드린다. 막으면 개발이 멈춘다."""
    after = admin.set_channel_mode(
        "tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "파일럿"), repo=repo
    )

    assert after.mode == ChannelMode.SHADOW


def test_active_works_once_the_schema_is_verified(repo, gate_open):
    repo.given_channel("tyit", "C1", "shadow")
    _ready_for_active(repo)

    after = admin.set_channel_mode(
        "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "인수"),
        cutover_ts="1700000000.0001", repo=repo,
    )

    assert after.mode == ChannelMode.ACTIVE
    assert after.writer_owner == WriterOwner.ARCHIVER


def test_the_gate_is_checked_before_the_transition(repo, gate_closed):
    """전이가 통과한 뒤에 막으면 「갈 수 있는데 안 보내 준다」 로 보인다.

    그때 사람은 규칙을 의심하고, 규칙을 의심하면 우회할 길을 찾는다.
    """
    repo.given_channel("tyit", "C1", "off")

    with pytest.raises(admin.AdminRefused) as caught:
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "급함"),
            cutover_ts="1700000000.0001", repo=repo,
        )

    # 전이 규칙(off→active 금지)이 아니라 **게이트** 사유가 먼저 나온다
    assert "검증" in str(caught.value)


@pytest.mark.parametrize("name", sorted(admin.GATED_FLAGS))
def test_turning_on_a_dangerous_flag_needs_the_gate(repo, gate_closed, name):
    """켜는 순간 운영 원문의 모양이 바뀌는 스위치들이다."""
    with pytest.raises(admin.AdminRefused, match="검증"):
        admin.set_feature_flag(name, True, admin.Actor("dan", "지금"), repo=repo)

    assert repo.flag_rows == {}


@pytest.mark.parametrize("name", sorted(admin.GATED_FLAGS))
def test_turning_a_dangerous_flag_off_is_never_blocked(repo, gate_closed, name):
    """**사고 때 내리는 손잡이를 검증 상태로 막으면, 막아야 할 순간에 못 막는다.**"""
    repo.given_flag(name, True)

    admin.set_feature_flag(name, False, admin.Actor("dan", "사고 대응"), repo=repo)

    assert repo.flag_rows[(name, "global", "")]["enabled"] is False


def test_the_detail_view_says_what_is_gated_and_why(repo, gate_closed):
    """눌러 보고 거절당하는 것보다 회색 버튼이 낫다."""
    repo.given_retention("bot_conversation_audit")
    repo.given_retention("bot_dm_attachment")

    detail = admin.workspace_detail("tyit", repo=repo)

    assert detail["schemaGate"]["verified"] is False
    assert "검증" in detail["schemaGate"]["reason"]
    assert "active" in detail["gatedModes"]
    assert "archiver_writes_live" in detail["gatedFlags"]


# --- 전이 함수를 지난다 ------------------------------------------------------

def test_an_illegal_transition_never_reaches_the_repository(repo, gate_open):
    """`off → active` 는 저장 전에 막힌다."""
    repo.given_channel("tyit", "C1", "off")

    with pytest.raises(admin.AdminRefused, match="off → active"):
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "급함"),
            cutover_ts="1700000000.0001", repo=repo,
        )

    assert repo.channel_rows[("tyit", "C1")]["mode"] == "off"
    assert repo.audit_rows == []


def test_mode_and_owner_move_together(repo, gate_open):
    repo.given_channel("tyit", "C1", "shadow")
    _ready_for_active(repo)

    admin.set_channel_mode(
        "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "인수"),
        cutover_ts="1700000000.0001", repo=repo,
    )

    saved = repo.channel_rows[("tyit", "C1")]
    assert (saved["mode"], saved["writer_owner"]) == ("active", "archiver")
    assert saved["cutover_ts"] == "1700000000.0001"


def test_the_owner_is_not_a_caller_argument():
    """호출부가 주인을 고를 수 있으면 전이 규칙이 장식이 된다."""
    import inspect

    assert "writer_owner" not in inspect.signature(admin.set_channel_mode).parameters


def test_the_transition_helper_is_called_from_exactly_one_place():
    """전이 계산이 여러 자리에 있으면 한쪽만 고치는 날이 온다."""
    import ast

    calls = [
        node for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plan_mode_change"
    ]

    assert len(calls) == 1, f"{len(calls)}곳에서 전이를 계산한다"


def test_this_module_contains_no_sql():
    """SQL 이 규칙과 섞이면 시험이 문장 모양을 지키게 된다."""
    for keyword in ("INSERT INTO", "UPDATE ", "SELECT ", "DELETE FROM"):
        assert keyword not in SOURCE, keyword


def test_a_reverse_cutover_goes_through_the_same_path(repo, gate_open):
    """역인수도 좌표를 요구한다. 콘솔이 그 규칙을 우회하지 않는다."""
    repo.given_channel("tyit", "C1", "paused", "archiver", "1700000000.0001")

    with pytest.raises(admin.AdminRefused, match="역인수 좌표"):
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "롤백"), repo=repo
        )


def test_an_unknown_channel_starts_from_off(repo, gate_open):
    """행이 없으면 `off` 에서 출발한다. 없는 채널을 바로 active 로 켤 수 없다."""
    with pytest.raises(admin.AdminRefused, match="off → active"):
        admin.set_channel_mode(
            "tyit", "C-NEW", ChannelMode.ACTIVE, admin.Actor("dan", "신규"),
            cutover_ts="1700000000.0001", repo=repo,
        )


def test_the_channel_row_is_locked_before_it_is_read(repo, gate_closed):
    """읽고 쓰는 사이에 남이 바꾸면 전이 판정이 옛 상태에서 나온다."""
    admin.set_channel_mode(
        "tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "파일럿"), repo=repo
    )

    assert repo.locked == [("tyit", "C1")]


def test_state_and_audit_use_one_transaction(repo, gate_closed):
    admin.set_channel_mode(
        "tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "파일럿"), repo=repo
    )

    assert repo.transaction_count == 1


def test_audit_failure_rolls_back_the_state_change(repo, gate_closed):
    repo.given_channel("tyit", "C1", "off")
    repo.fail_audit = True

    with pytest.raises(RuntimeError, match="audit failed"):
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.SHADOW,
            admin.Actor("dan", "파일럿"), repo=repo,
        )

    assert repo.channel_rows[("tyit", "C1")]["mode"] == "off"


def test_active_is_blocked_while_production_policy_is_incomplete(repo, gate_open):
    repo.given_channel("tyit", "C1", "shadow")

    with pytest.raises(admin.AdminRefused, match="운영 전환 조건"):
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.ACTIVE,
            admin.Actor("dan", "인수"), cutover_ts="1700000000.0001", repo=repo,
        )

    assert repo.channel_rows[("tyit", "C1")]["mode"] == "shadow"


# --- 기능 스위치 -------------------------------------------------------------

@pytest.mark.parametrize(
    ("scope", "scope_key"),
    [("global", "tyit"), ("workspace", ""), ("channel", "")],
    ids=["global-with-key", "workspace-without-key", "channel-without-key"],
)
def test_flag_scope_and_target_must_agree(repo, gate_closed, scope, scope_key):
    """범위와 대상이 어긋나면 그 스위치가 무엇에 걸리는지 아무도 모른다."""
    with pytest.raises(admin.AdminRefused, match="대상"):
        admin.set_feature_flag(
            "require_attachment_ack", True, admin.Actor("dan", "파일럿"),
            scope=scope, scope_key=scope_key, repo=repo,
        )


def test_an_unknown_scope_is_refused(repo, gate_closed):
    with pytest.raises(admin.AdminRefused, match="범위"):
        admin.set_feature_flag(
            "x", True, admin.Actor("dan", "왜"), scope="everywhere", scope_key="k",
            repo=repo,
        )


def test_turning_a_flag_on_is_audited_with_the_old_value(repo, gate_closed):
    """이전 값이 없으면 감사를 보고 「무엇이 바뀌었나」 를 알 수 없다."""
    repo.given_flag("require_attachment_ack", False, "workspace", "tyit")

    admin.set_feature_flag(
        "require_attachment_ack", True, admin.Actor("dan", "ACK 준비됨"),
        scope="workspace", scope_key="tyit", repo=repo,
    )

    entry = repo.audit_rows[-1]
    assert (entry["old_value"], entry["new_value"]) == ("false", "true")
    assert entry["workspace"] == "tyit"


def test_attachment_separation_requires_the_global_reader(repo, gate_open):
    with pytest.raises(admin.AdminRefused, match="reader 준비"):
        admin.set_feature_flag(
            "separate_attachments", True, admin.Actor("dan", "분리"), repo=repo
        )

    repo.given_flag("attachment_reader_ready", True)
    admin.set_feature_flag(
        "separate_attachments", True, admin.Actor("dan", "분리"), repo=repo
    )
    assert repo.flag_rows[("separate_attachments", "global", "")]["enabled"] is True


def test_workspace_scope_is_bound_to_the_request_workspace(repo, gate_closed):
    with pytest.raises(admin.AdminRefused, match="다른 워크스페이스"):
        admin.set_feature_flag(
            "require_attachment_ack", True, admin.Actor("dan", "설정"),
            scope="workspace", scope_key="mgmt", workspace_context="tyit", repo=repo,
        )


def test_channel_scope_must_belong_to_the_request_workspace(repo, gate_closed):
    repo.given_channel("mgmt", "C-MGMT", "shadow")

    with pytest.raises(admin.AdminRefused, match="없는 채널"):
        admin.set_feature_flag(
            "require_attachment_ack", True, admin.Actor("dan", "설정"),
            scope="channel", scope_key="C-MGMT", workspace_context="tyit", repo=repo,
        )


# --- 보존 정책 ---------------------------------------------------------------

@pytest.mark.parametrize("days", [0, -1])
def test_a_non_positive_retention_is_refused(repo, gate_closed, days):
    """기록이 생기자마자 사라지면 감사 계약이 성립하지 않는다."""
    repo.given_retention("bot_conversation_audit")

    with pytest.raises(admin.AdminRefused, match="1일 이상"):
        admin.set_retention("bot_conversation_audit", days, admin.Actor("dan", "법무"))


def test_an_unknown_policy_is_refused(repo, gate_closed):
    """없는 이름을 조용히 만들면 게이트가 보는 행과 다른 행이 생긴다."""
    with pytest.raises(admin.AdminRefused, match="없는 보존 정책"):
        admin.set_retention("made_up", 30, admin.Actor("dan", "실수"), repo=repo)

    assert repo.retention_rows == {}


def test_setting_a_policy_records_who_approved_it(repo, gate_closed):
    """사람이 없으면 나중에 그 값을 바꿔도 되는지 아무도 모른다."""
    repo.given_retention("bot_conversation_audit")

    admin.set_retention(
        "bot_conversation_audit", 90, admin.Actor("dan", "법무 승인"), repo=repo
    )

    assert repo.retention_rows["bot_conversation_audit"]["retention_days"] == 90
    assert repo.retention_rows["bot_conversation_audit"]["approved_by"] == "dan"


def test_clearing_a_policy_is_also_recorded(repo, gate_closed):
    """되돌리는 것도 결정이다. 기록이 없으면 왜 비었는지 모른다."""
    repo.given_retention("bot_conversation_audit", 90)

    admin.set_retention(
        "bot_conversation_audit", None, admin.Actor("dan", "재검토"), repo=repo
    )

    assert repo.audit_rows[-1]["old_value"] == "90"
    assert repo.audit_rows[-1]["new_value"] == ""
    assert repo.retention_rows["bot_conversation_audit"]["approved_by"] == ""


def test_setting_retention_unblocks_production(repo, gate_closed):
    """게이트가 실제로 열리는지 본다 — 안 열리면 목록이 장식이다."""
    repo.given_retention("bot_conversation_audit", 90)
    repo.given_retention("bot_dm_attachment", 30)
    for name in (
        "archiver_writes_live",
        "preserve_edit_delete",
        "require_attachment_ack",
        "revision_reader_ready",
    ):
        repo.given_flag(name, True)

    assert admin.workspace_detail("tyit", repo=repo)["blockers"] == []


def test_only_global_flags_feed_the_gate():
    """파일럿 한 채널이 전체 판정을 뒤집으면 안 된다."""
    flags = [
        {"name": "separate_attachments", "scope": "global", "enabled": False},
        {"name": "separate_attachments", "scope": "channel", "enabled": True},
    ]

    assert admin._flag_map(flags) == {"separate_attachments": False}
