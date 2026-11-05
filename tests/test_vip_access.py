from __future__ import annotations

from dataclasses import replace

from tybot import bot_access, vip_access
from tybot.access import RequestContext, can_access
from tybot.archive.store import ArchiveStore


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        normalized = " ".join(sql.lower().split())
        if "from user_identity" in normalized:
            self.rows = [{"emp_no": self.conn.emp_no}] if self.conn.emp_no else []
        elif "from employee" in normalized:
            self.rows = [self.conn.grant] if self.conn.grant else []
        elif "from bot_source_channel" in normalized:
            self.rows = [
                {"workspace": workspace, "channel_id": channel_id}
                for workspace, channel_id in self.conn.sources
            ]
        else:  # pragma: no cover - 새 SQL은 시험 대역도 명시적으로 갱신해야 한다
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, *, emp_no="E1", grant=None, sources=()):
        self.emp_no = emp_no
        self.grant = grant or {
            "emp_no": emp_no,
            "active": True,
            "grant_id": 7,
            "expires_at": None,
            "live": True,
        }
        self.sources = tuple(sources)

    def cursor(self):
        return _Cursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        pass


class _Slack:
    def users_info(self, *, user):  # pragma: no cover - existing mappings avoid this call
        raise AssertionError(user)


def _doc(*, workspace="mgmt", channel_id="C0EXEC001", dm_user=None):
    return {
        "visibility": "private",
        "acl": frozenset({"#질문자가_없는채널"}),
        "owner_workspace": workspace,
        "channel_id": channel_id,
        "channel": "#경영_근거",
        "dm_user": dm_user,
    }


def test_authorized_user_gets_only_the_registered_source_coordinates():
    auth = vip_access.authorize(
        _Conn(sources=(("mgmt", "C0EXEC001"), ("tyit", "C0IT0001"))),
        workspace="tyit",
        slack_user="U1",
    )
    assert auth.allowed and auth.code == vip_access.ALLOWED
    assert auth.emp_no == "E1" and auth.grant_id == 7
    assert auth.context().evidence_sources == {
        ("mgmt", "C0EXEC001"),
        ("tyit", "C0IT0001"),
    }


def test_registered_source_ignores_human_membership_but_requires_both_coordinates():
    ctx = RequestContext(
        workspace="tyit",
        channels=frozenset(),
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    assert can_access(ctx, **_doc())
    assert not can_access(ctx, **_doc(channel_id="C0OTHER1"))
    assert not can_access(ctx, **_doc(workspace="tyit"))


def test_public_or_root_permissions_cannot_expand_a_vip_source_list():
    ctx = RequestContext(
        workspace="tyit",
        role="exec",
        is_root=True,
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    other = _doc(channel_id="C0OTHER1")
    other["visibility"] = "public"
    other["acl"] = None
    assert not can_access(ctx, **other)


def test_workspace_reachability_comes_from_registered_sources_only():
    ctx = RequestContext(
        workspace="tyit",
        role="exec",
        is_root=True,
        readable_workspaces=frozenset({"other"}),
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    assert ctx.may_reach("mgmt")
    assert not ctx.may_reach("tyit")
    assert not ctx.may_reach("other")


def test_existing_request_context_copy_preserves_managed_sources():
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    base = RequestContext(
        workspace="tyit",
        user_id="U1",
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    bot._context = lambda _client, _user: base
    bot._channel_name = lambda _client, _channel: "#호출채널"

    copied = bot._request_context(
        object(), "U1", channel_id="C0CALL001", in_channel=True
    )
    assert copied.evidence_sources == base.evidence_sources
    assert copied.channel_id == "C0CALL001"


def test_vip_source_scope_never_opens_a_personal_dm():
    ctx = RequestContext(
        workspace="tyit",
        user_id="U1",
        evidence_sources=frozenset({("tyit", "D0PERSONAL")}),
    )
    assert not can_access(
        ctx,
        visibility="private",
        acl=frozenset(),
        owner_workspace="tyit",
        channel_id="D0PERSONAL",
        channel="@U1",
        dm_user="U1",
    )


def test_empty_source_list_closes_workspace_reachability_too():
    """`can_access` 만 닫으면 **1차 필터가 열린 채로 남는다.**

    `may_reach` 는 답변 전에 워크스페이스를 고르는 자리다. 빈 목록을 「관리형이
    아니다」 로 읽으면 exec/root 규칙으로 되돌아가고, 그러면 근거가 하나도
    등록되지 않은 VIP 요청이 조직 전체를 후보로 삼은 뒤 문서 단위에서만 걸린다.
    """
    ctx = RequestContext(
        workspace="tyit",
        role="exec",
        is_root=True,
        readable_workspaces=frozenset({"other"}),
        evidence_sources=frozenset(),
    )
    assert not ctx.may_reach("tyit")
    assert not ctx.may_reach("other")
    assert not ctx.may_reach("mgmt")


def test_empty_source_list_is_an_explicit_deny_even_for_public_material():
    ctx = RequestContext(
        workspace="tyit", evidence_sources=frozenset(), role="exec", is_root=True
    )
    doc = _doc()
    doc["visibility"] = "public"
    assert not can_access(ctx, **doc)


def test_no_sources_denies_before_a_context_can_be_built():
    auth = vip_access.authorize(_Conn(sources=()), workspace="tyit", slack_user="U1")
    assert not auth.allowed and auth.code == vip_access.NO_SOURCES
    try:
        auth.context()
    except ValueError as exc:
        assert str(exc) == vip_access.NO_SOURCES
    else:  # pragma: no cover
        raise AssertionError("denied authorization produced a context")


def test_bot_access_denial_never_reads_or_returns_sources():
    conn = _Conn(
        grant={
            "emp_no": "E1",
            "active": True,
            "grant_id": None,
            "expires_at": None,
            "live": False,
        },
        sources=(("mgmt", "C0EXEC001"),),
    )
    auth = vip_access.authorize(conn, workspace="tyit", slack_user="U1")
    assert not auth.allowed and auth.code == bot_access.NO_GRANT
    assert auth.sources == frozenset()


def test_slack_entrypoint_reuses_the_verified_identity_and_same_connection():
    conn = _Conn(sources=(("mgmt", "C0EXEC001"),))
    calls = 0

    def connect():
        nonlocal calls
        calls += 1
        return conn

    auth = vip_access.authorize_slack(
        connect=connect,
        client=_Slack(),
        workspace="tyit",
        slack_user="U1",
    )
    assert auth.allowed and auth.sources == {("mgmt", "C0EXEC001")}
    assert calls == 1


def test_slack_entrypoint_converts_database_failure_to_a_closed_decision():
    def broken():
        raise RuntimeError("password=must-not-escape")

    auth = vip_access.authorize_slack(
        connect=broken,
        client=_Slack(),
        workspace="tyit",
        slack_user="U1",
    )
    assert auth == vip_access.Authorization(
        False, bot_access.CHECK_FAILED, "tyit", "U1"
    )


def test_normal_request_context_keeps_the_existing_acl_rules():
    member = RequestContext(workspace="mgmt", channels=frozenset({"#내채널"}))
    assert can_access(
        member,
        visibility="private",
        acl=frozenset({"#내채널"}),
        owner_workspace="mgmt",
        channel_id="C0NORMAL1",
    )
    assert not can_access(member, **_doc())


def _archive_doc(workspace: str, channel_id: str, channel: str, text: str) -> str:
    return (
        "---\n"
        f"workspace: {workspace}\n"
        f'channel: "{channel}"\n'
        f"channel_id: {channel_id}\n"
        "visibility: public\n"
        "acl: []\n"
        "doc_count: 1\n"
        "last_ingested: 2026-11-05T09:00+09:00\n"
        "---\n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
        f"> [2026-11-05 09:00] 담당자: {text}\n"
    )


def test_archive_search_uses_only_the_exact_registered_coordinates(tmp_path):
    docs = (
        ("mgmt", "C0EXEC001", "#경영근거", "VIP검색어 등록자료"),
        ("mgmt", "C0OTHER1", "#다른채널", "VIP검색어 미등록자료"),
        ("tyit", "C0EXEC001", "#같은ID다른WS", "VIP검색어 다른워크스페이스"),
    )
    for workspace, channel_id, channel, text in docs:
        path = tmp_path / "channels" / workspace / f"{channel_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _archive_doc(workspace, channel_id, channel, text), encoding="utf-8"
        )

    ctx = RequestContext(
        workspace="tyit",
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    hits = ArchiveStore(tmp_path).search("VIP검색어", ctx, limit=20)
    assert [hit.line.text for hit in hits] == ["VIP검색어 등록자료"]

    from tybot.evidence_refs import ref_from_hit

    ref = ref_from_hit(hits[0], tmp_path)
    reopened, dropped = ArchiveStore(tmp_path).resolve_refs([ref], ctx)
    assert [hit.line.text for hit in reopened] == ["VIP검색어 등록자료"]
    assert dropped == []


def test_unregistered_reference_is_rejected_before_path_lookup(tmp_path):
    path = tmp_path / "channels" / "mgmt" / "C0OTHER1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _archive_doc("mgmt", "C0OTHER1", "#다른채널", "숨겨진 자료"),
        encoding="utf-8",
    )
    member = RequestContext(workspace="mgmt", channels=frozenset({"#다른채널"}))
    store = ArchiveStore(tmp_path)
    from tybot.evidence_refs import ref_from_hit

    ref = ref_from_hit(store.search("숨겨진", member)[0], tmp_path)
    vip_ctx = RequestContext(
        workspace="tyit",
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )
    reopened, dropped = store.resolve_refs([ref], vip_ctx)
    assert reopened == []
    assert dropped == ["bot_source_scope"]


def test_reference_workspace_must_match_the_reopened_document(tmp_path):
    path = tmp_path / "channels" / "mgmt" / "C0SHARED1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _archive_doc("mgmt", "C0SHARED1", "#경영근거", "워크스페이스 좌표"),
        encoding="utf-8",
    )
    member = RequestContext(workspace="mgmt", channels=frozenset({"#경영근거"}))
    store = ArchiveStore(tmp_path)
    from tybot.evidence_refs import ref_from_hit

    ref = ref_from_hit(store.search("워크스페이스 좌표", member)[0], tmp_path)
    forged = replace(ref, workspace="tyit")
    vip_ctx = RequestContext(
        workspace="tyit",
        evidence_sources=frozenset(
            {("mgmt", "C0SHARED1"), ("tyit", "C0SHARED1")}
        ),
    )
    reopened, dropped = store.resolve_refs([forged], vip_ctx)
    assert reopened == []
    assert dropped == ["workspace_coordinate"]
