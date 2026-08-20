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


def _ctx(ws="pilot", channels=("#팀_자금(ABB540)_주간보고",), readable=(), role="member",
         is_root=False):
    return RequestContext(
        workspace=ws,
        channels=frozenset(channels),
        role=role,
        readable_workspaces=frozenset(readable),
        is_root=is_root,
    )


def test_cross_workspace_blocked_without_whitelist():
    assert not can_access(
        _ctx(ws="mgmt", channels=()),
        visibility="public",
        acl=None,
        owner_workspace="pilot",
    )


def test_root_workspace_reads_subordinate_regardless_of_flags():
    """상위(root) 워크스페이스는 산하 자료를 문서 표시와 무관하게 열람한다."""
    root = _ctx(ws="mgmt", channels=(), readable=("pilot",), is_root=True)
    assert can_access(root, visibility="private", acl=None, owner_workspace="pilot")
    assert can_access(root, visibility=None, acl=frozenset({"#남의채널"}), owner_workspace="pilot")


def test_peer_workspace_needs_explicit_share_with():
    """동등 워크스페이스는 화이트리스트만으로 부족하다 - 문서가 지목해야 넘어간다."""
    peer = _ctx(ws="team_b", channels=(), readable=("pilot",))
    assert not can_access(peer, visibility="public", acl=None, owner_workspace="pilot")
    assert can_access(
        peer, visibility="private", acl=None, owner_workspace="pilot",
        share_with=frozenset({"team_b"}),
    )
    # 다른 워크스페이스를 지목한 문서는 넘어가지 않는다
    assert not can_access(
        peer, visibility="private", acl=None, owner_workspace="pilot",
        share_with=frozenset({"mgmt"}),
    )


def test_public_no_longer_crosses_workspaces():
    """visibility: public 은 자기 워크스페이스 안에서만 유효하다.

    예전에는 이 하나가 크로스 열람까지 열어서, 화이트리스트에 제3의 동등 워크스페이스가
    추가되면 그쪽에도 자료가 나갔다.
    """
    peer = _ctx(ws="team_b", channels=(), readable=("pilot",))
    assert not can_access(peer, visibility="public", acl=None, owner_workspace="pilot")


def test_non_member_channel_blocked_even_if_public_channel_in_slack():
    """같은 워크스페이스라도 소속되지 않은 채널은 답하지 않는다.

    Slack 에서 공개 채널이어도, 그 채널에 들어가 있지 않은 사람이 봇으로 우회 열람하면 안 된다.
    """
    ctx = _ctx(ws="pilot", channels=("#내가_있는채널",))
    assert not can_access(
        ctx, visibility="private", acl=frozenset({"#남의_공개채널"}), owner_workspace="pilot"
    )
    assert can_access(
        ctx, visibility="private", acl=frozenset({"#내가_있는채널"}), owner_workspace="pilot"
    )


def test_root_workspace_ignores_channel_membership_in_own_workspace():
    """취합·열람 전담 워크스페이스는 자기 워크스페이스 안에서 멤버십 필터를 받지 않는다."""
    root = _ctx(ws="mgmt", channels=(), is_root=True)
    assert can_access(
        root, visibility="private", acl=frozenset({"#경영_어느채널"}), owner_workspace="mgmt"
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


def test_root_search_sees_all_subordinate_docs(store):
    """상위 워크스페이스는 산하 자료를 표시와 무관하게 검색한다."""
    ctx = _ctx(ws="mgmt", channels=(), readable=("pilot",), is_root=True)
    texts = [h.line.text for h in store.search("기성금", ctx)]
    assert "공개 기성금 3억" in texts
    assert "비밀 기성금 9억" in texts  # root 는 private 도 본다
    assert "경영 기성금 5억" in texts  # 자기 워크스페이스, 멤버십 무관


def test_peer_search_sees_nothing_without_share_with(store):
    """동등 워크스페이스는 문서가 지목하지 않으면 아무것도 못 본다."""
    ctx = _ctx(ws="team_b", channels=(), readable=("pilot",))
    assert store.search("기성금", ctx) == []


def test_member_search_limited_to_own_channels(store):
    """일반 워크스페이스 사용자는 자기 채널만 본다(공개 표시 문서는 예외)."""
    ctx = _ctx(ws="pilot", channels=("#파일럿_비공개",))
    texts = [h.line.text for h in store.search("기성금", ctx)]
    assert "비밀 기성금 9억" in texts  # 소속 채널
    assert "공개 기성금 3억" in texts  # visibility: public (사람이 명시)
    assert "경영 기성금 5억" not in texts  # 타 워크스페이스


def test_titles_hide_unreachable_workspaces(store):
    ctx = _ctx(ws="pilot", channels=("#파일럿_비공개",), readable=())
    titles = store.titles(ctx)
    assert "#경영_내부" not in titles  # 권한 없으면 채널명도 노출하지 않는다


# --- 크로스 조회 답변 경로 ------------------------------------------------


class _Fake:
    name = "anthropic"

    def __init__(self):
        self.calls = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        from tybot.gateway.base import LLMResponse

        self.calls.append(list(messages))
        return LLMResponse("정리 결과", spec.model, self.name, 200, 40, 0.001)


def _engine(tmp_path):
    from tybot.answer import AnswerEngine
    from tybot.gateway.base import ModelSpec, Sensitivity
    from tybot.gateway.cost import CostGuard
    from tybot.gateway.router import Router

    fake = _Fake()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router), fake


def _today_doc(ws, channel, visibility, line):
    import datetime as dt

    return _doc(ws, channel, visibility, line).replace(
        "2026-08-19 09:00", f"{dt.date.today()} 09:00"
    )


def test_summary_marks_other_workspace_in_sources(tmp_path):
    """상위 워크스페이스가 산하 자료를 정리할 때 출처에 워크스페이스가 붙는다."""
    """다른 워크스페이스 자료는 출처와 근거에 워크스페이스가 표기돼야 한다."""
    base = tmp_path / "channels"
    (base / "pilot").mkdir(parents=True)
    (base / "mgmt").mkdir(parents=True)
    (base / "pilot" / "공개.md").write_text(
        _today_doc("pilot", "#파일럿_공개", "public", "기성금 3억 청구"), encoding="utf-8"
    )
    (base / "mgmt" / "경영.md").write_text(
        _today_doc("mgmt", "#경영_내부", "private", "경영 회의 진행"), encoding="utf-8"
    )
    eng, fake = _engine(tmp_path)
    ctx = _ctx(ws="mgmt", channels=("#경영_내부",), readable=("pilot",), is_root=True)

    ans = eng.summarize(ctx, days=7)
    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "[pilot] 채널 #파일럿_공개" in evidence  # 근거에 소유 워크스페이스 표기
    assert "### 채널 #경영_내부" in evidence  # 자기 워크스페이스는 태그 없음
    assert any(c.startswith("[pilot] ") for c in ans.citations)


def test_scope_question_is_not_refused(tmp_path):
    """'다른 워크스페이스 내용 알려줘' 를 외부 정보 질문으로 거절하지 않는다."""
    from tybot.intent import Intent

    base = tmp_path / "channels" / "pilot"
    base.mkdir(parents=True)
    (base / "공개.md").write_text(
        _today_doc("pilot", "#파일럿_공개", "public", "기성금 3억 청구"), encoding="utf-8"
    )
    eng, fake = _engine(tmp_path)
    ctx = _ctx(ws="mgmt", channels=(), readable=("pilot",), is_root=True)

    ans = eng.respond("현재 다른 워크스페이스의 내용을 알려줘", ctx, Intent("out_of_scope"))
    assert ans.reason == "answered"
    assert fake.calls  # 거절이 아니라 정리로 답한다


def test_truly_unrelated_question_still_refused(tmp_path):
    from tybot.intent import Intent

    eng, fake = _engine(tmp_path)
    ans = eng.respond("내일 날씨 어때?", _ctx(ws="mgmt", channels=()), Intent("out_of_scope"))
    assert ans.reason == "out_of_scope"
    assert fake.calls == []
