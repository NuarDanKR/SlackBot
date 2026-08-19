"""멀티 워크스페이스 — 설정 파싱과 크로스 워크스페이스 격리.

주의: 가짜 토큰에도 실제 토큰 패턴(`xoxb-` + 영숫자 10자 이상)을 쓰지 않는다.
저장소 가드 훅이 시크릿으로 오인해 커밋을 막는다.
"""
from __future__ import annotations

import pytest

from tybot.access import RequestContext, can_access
from tybot.archive.store import ArchiveStore
from tybot.workspaces import ConfigError, load_workspaces, parse_cross_read

BOT = "xoxb-fake_1"
APP = "xapp-fake_1"

# --- 설정 로딩 -----------------------------------------------------------


def test_single_workspace_backward_compatible():
    cfgs = load_workspaces(
        {"PILOT_WORKSPACE": "pilot", "SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP}
    )
    assert [c.key for c in cfgs] == ["pilot"]
    assert cfgs[0].readable == frozenset()  # 기본은 크로스 차단


def test_multi_workspace_tokens_per_key():
    cfgs = load_workspaces(
        {
            "WORKSPACES": "pilot, mgmt",
            "SLACK_BOT_TOKEN_PILOT": "xoxb-p_1",
            "SLACK_APP_TOKEN_PILOT": "xapp-p_1",
            "SLACK_BOT_TOKEN_MGMT": "xoxb-m_1",
            "SLACK_APP_TOKEN_MGMT": "xapp-m_1",
            "WORKSPACE_LABEL_MGMT": "경영본부",
            "CROSS_WS_READ": "mgmt:pilot",
        }
    )
    by = {c.key: c for c in cfgs}
    assert by["pilot"].bot_token == "xoxb-p_1" and by["mgmt"].app_token == "xapp-m_1"
    assert by["mgmt"].label == "경영본부"
    # 단방향이다 — mgmt 는 pilot 을 읽지만 pilot 은 mgmt 를 못 읽는다
    assert by["mgmt"].readable == frozenset({"pilot"})
    assert by["pilot"].readable == frozenset()


def test_missing_token_stops_startup():
    with pytest.raises(ConfigError, match="토큰 누락"):
        load_workspaces({"WORKSPACES": "a,b", "SLACK_BOT_TOKEN_A": BOT, "SLACK_APP_TOKEN_A": APP})


def test_masked_config_does_not_leak_token():
    cfg = load_workspaces(
        {"PILOT_WORKSPACE": "p", "SLACK_BOT_TOKEN": "xoxb-abc_MUSTNOTAPPEAR", "SLACK_APP_TOKEN": APP}
    )[0]
    assert "MUSTNOTAPPEAR" not in cfg.masked()


@pytest.mark.parametrize("spec", ["mgmt", "mgmt:없는키", "없는키:mgmt"])
def test_bad_cross_spec_is_rejected(spec):
    """오타 하나가 조용히 권한을 열거나 닫는 상황을 막는다."""
    with pytest.raises(ConfigError):
        parse_cross_read(spec, {"mgmt", "pilot"})


def test_wildcard_expands_and_excludes_self():
    assert parse_cross_read("mgmt:*", {"mgmt", "a", "b"}) == {"mgmt": frozenset({"a", "b"})}


# --- 권한 판정 -----------------------------------------------------------


def _ctx(ws="pilot", channels=("#팀_자금(ABB540)_주간보고",), readable=(), role="member"):
    return RequestContext(
        workspace=ws,
        channels=frozenset(channels),
        role=role,
        readable_workspaces=frozenset(readable),
    )


def test_cross_workspace_blocked_without_whitelist():
    assert not can_access(
        _ctx(ws="mgmt", channels=()),
        visibility="public",
        acl=None,
        owner_workspace="pilot",
    )


def test_cross_workspace_allows_only_public_documents():
    ctx = _ctx(ws="mgmt", channels=(), readable=("pilot",))
    assert can_access(ctx, visibility="public", acl=None, owner_workspace="pilot")
    # 화이트리스트가 있어도 비공개 문서는 나가지 않는다 — 무엇을 공개할지는 소유 쪽이 정한다
    assert not can_access(ctx, visibility="private", acl=None, owner_workspace="pilot")
    assert not can_access(ctx, visibility=None, acl=None, owner_workspace="pilot")


def test_cross_workspace_ignores_channel_acl():
    """acl 은 소유 워크스페이스 안의 채널 목록이다.

    수집기가 acl=[채널명] 을 항상 넣으므로, 크로스 판정에 acl 을 걸면 `visibility: public`
    표시가 무력해진다. 크로스는 화이트리스트 + 공개 표시 두 관문으로만 판정한다.
    """
    ctx = _ctx(ws="mgmt", channels=(), readable=("pilot",))
    assert can_access(
        ctx, visibility="public", acl=frozenset({"#파일럿_공개"}), owner_workspace="pilot"
    )
    assert not can_access(
        ctx, visibility="private", acl=frozenset({"#파일럿_공개"}), owner_workspace="pilot"
    )


def test_whitelist_is_one_directional():
    """mgmt→pilot 허용이 pilot→mgmt 허용을 의미하지 않는다."""
    pilot = _ctx(ws="pilot", channels=(), readable=())
    assert not can_access(pilot, visibility="public", acl=None, owner_workspace="mgmt")


def test_own_workspace_rules_unchanged():
    ctx = _ctx()
    assert can_access(
        ctx,
        visibility="private",
        acl=frozenset({"#팀_자금(ABB540)_주간보고"}),
        owner_workspace="pilot",
    )
    assert not can_access(
        ctx, visibility="private", acl=frozenset({"#다른채널"}), owner_workspace="pilot"
    )


def test_exec_crosses_workspaces():
    ctx = _ctx(ws="mgmt", channels=(), role="exec")
    assert can_access(ctx, visibility="private", acl=None, owner_workspace="pilot")


# --- 검색 경로에서의 격리 ------------------------------------------------


def _doc(ws, channel, visibility, line):
    return (
        "---\n"
        f"workspace: {ws}\n"
        f'channel: "{channel}"\n'
        f"visibility: {visibility}\n"
        f"acl: [{channel}]\n"
        "doc_count: 1\n"
        "last_ingested: 2026-08-19T17:00+09:00\n"
        "---\n\n## 요약\n-\n\n## 원문 (자동 취합, 편집 금지)\n"
        f"> [2026-08-19 09:00] 홍길동: {line}\n"
    )


@pytest.fixture
def store(tmp_path):
    base = tmp_path / "channels"
    (base / "pilot").mkdir(parents=True)
    (base / "mgmt").mkdir(parents=True)
    (base / "pilot" / "공개.md").write_text(
        _doc("pilot", "#파일럿_공개", "public", "공개 기성금 3억"), encoding="utf-8"
    )
    (base / "pilot" / "비공개.md").write_text(
        _doc("pilot", "#파일럿_비공개", "private", "비밀 기성금 9억"), encoding="utf-8"
    )
    (base / "mgmt" / "경영.md").write_text(
        _doc("mgmt", "#경영_내부", "private", "경영 기성금 5억"), encoding="utf-8"
    )
    return ArchiveStore(tmp_path)


def test_search_crosses_only_public_docs(store):
    ctx = _ctx(ws="mgmt", channels=("#경영_내부",), readable=("pilot",))
    texts = [h.line.text for h in store.search("기성금", ctx)]
    assert "공개 기성금 3억" in texts  # 화이트리스트 + 공개
    assert "경영 기성금 5억" in texts  # 자기 워크스페이스 채널 멤버
    assert "비밀 기성금 9억" not in texts  # 타 워크스페이스 비공개


def test_titles_hide_unreachable_workspaces(store):
    ctx = _ctx(ws="pilot", channels=("#파일럿_비공개",), readable=())
    titles = store.titles(ctx)
    assert "#경영_내부" not in titles  # 권한 없으면 채널명도 노출하지 않는다
