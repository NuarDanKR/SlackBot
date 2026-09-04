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


def test_root_workspace_reaches_every_workspace_without_cross_read_entries():
    root = _ctx(ws="mgmt", channels=(), readable=(), is_root=True)
    assert can_access(root, visibility="private", acl=None, owner_workspace="pilot")
    assert can_access(root, visibility="private", acl=None, owner_workspace="tyit")


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


def test_summary_named_workspace_excludes_other_visible_workspaces(tmp_path, monkeypatch):
    """특정 워크스페이스를 물으면 root가 볼 수 있는 다른 자료를 근거에 섞지 않는다."""
    base = tmp_path / "channels"
    for workspace, channel, line in (
        ("pilot", "#파일럿_공개", "파일럿 업무"),
        ("mgmt", "#경영_내부", "경영 업무"),
        ("tyit", "#전산_업무", "전산팀 업무"),
    ):
        path = base / workspace / f"{workspace}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_today_doc(workspace, channel, "private", line), encoding="utf-8")
    monkeypatch.setenv("WORKSPACES", "pilot,mgmt,tyit")
    monkeypatch.setenv("WORKSPACE_LABEL_PILOT", "파일럿")
    monkeypatch.setenv("WORKSPACE_LABEL_MGMT", "경영본부")
    monkeypatch.setenv("WORKSPACE_LABEL_TYIT", "전산팀")
    eng, fake = _engine(tmp_path)
    ctx = _ctx(ws="mgmt", channels=(), readable=("pilot", "tyit"), is_root=True)

    from tybot.intent import Intent

    ans = eng.respond("지금 전산팀 워크스페이스에서는 무슨 일이 벌어지고 있어?", ctx, Intent("summary"))

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "전산팀 업무" in evidence
    assert "파일럿 업무" not in evidence
    assert "경영 업무" not in evidence
    assert all(c.startswith("[tyit] ") for c in ans.citations)


def test_named_workspace_filter_uses_db_metadata_without_workspace_env(monkeypatch):
    from tybot.answer import _mentioned_workspaces
    from tybot.console import workspace_store

    monkeypatch.delenv("WORKSPACES", raising=False)
    monkeypatch.setattr(
        workspace_store,
        "list_workspaces",
        lambda: [
            {"key": "tyit", "label": "전산팀", "state": "enabled"},
            {"key": "old", "label": "폐쇄 조직", "state": "disabled"},
        ],
    )

    assert _mentioned_workspaces("전산팀 자료만 알려줘") == frozenset({"tyit"})
    assert not _mentioned_workspaces("폐쇄 조직 자료")


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


def test_db_registry_adds_complete_workspace_without_replacing_env_fallback(monkeypatch):
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store,
        "runtime_workspaces",
        lambda: [
            {
                "key": "tyit",
                "label": "전산팀",
                "role": "member",
                "state": "enabled",
                "bot_token": "xoxb-db_1",
                "app_token": "xapp-db_1",
                "readable": ["mgmt"],
            }
        ],
    )
    configs = load_workspaces(
        {
            "WORKSPACES": "mgmt",
            "SLACK_BOT_TOKEN_MGMT": "xoxb-env_1",
            "SLACK_APP_TOKEN_MGMT": "xapp-env_1",
            "DATABASE_URL": "postgresql://unused",
            "WORKSPACE_SECRET_KEY": "test-key-present",
        }
    )

    assert [config.key for config in configs] == ["mgmt", "tyit"]
    tyit = next(config for config in configs if config.key == "tyit")
    assert tyit.label == "전산팀"
    assert tyit.readable == frozenset({"mgmt"})


def test_incomplete_db_registry_row_keeps_existing_environment_workspace(monkeypatch):
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store,
        "runtime_workspaces",
        lambda: [
            {
                "key": "tyit",
                "label": "DB 전산팀",
                "role": "member",
                "state": "enabled",
                "bot_token": None,
                "app_token": None,
                "readable": [],
            }
        ],
    )
    configs = load_workspaces(
        {
            "WORKSPACES": "tyit",
            "SLACK_BOT_TOKEN_TYIT": "xoxb-env_1",
            "SLACK_APP_TOKEN_TYIT": "xapp-env_1",
            "WORKSPACE_LABEL_TYIT": "환경 전산팀",
            "DATABASE_URL": "postgresql://unused",
            "WORKSPACE_SECRET_KEY": "test-key-present",
        }
    )

    assert len(configs) == 1
    assert configs[0].label == "환경 전산팀"
    assert configs[0].bot_token == "xoxb-env_1"


def test_complete_db_registry_starts_without_workspace_environment(monkeypatch):
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store,
        "runtime_workspaces",
        lambda: [
            {
                "key": "tyit",
                "label": "전산팀",
                "role": "member",
                "state": "enabled",
                "bot_token": "xoxb-db_1",
                "app_token": "xapp-db_1",
                "readable": [],
            }
        ],
    )

    configs = load_workspaces(
        {
            "DATABASE_URL": "postgresql://unused",
            "WORKSPACE_SECRET_KEY": "test-key-present",
        }
    )

    assert [(config.key, config.label) for config in configs] == [("tyit", "전산팀")]
    assert configs[0].bot_token == "xoxb-db_1"


def test_incomplete_db_registry_without_environment_fails(monkeypatch):
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store,
        "runtime_workspaces",
        lambda: [
            {
                "key": "tyit",
                "label": "전산팀",
                "role": "member",
                "state": "enabled",
                "bot_token": None,
                "app_token": None,
                "readable": [],
            }
        ],
    )

    with pytest.raises(ConfigError, match="tyit"):
        load_workspaces(
            {
                "DATABASE_URL": "postgresql://unused",
                "WORKSPACE_SECRET_KEY": "test-key-present",
            }
        )


# --- 복호화 실패는 '토큰 미등록' 이 아니다 -------------------------------------
#
# 콘솔에서 토큰을 등록하면 DB 에는 두 개가 다 들어간다. 그런데 등록 당시의
# WORKSPACE_SECRET_KEY 와 지금 키가 다르면 읽지 못한다. 예전에는 그 행을 조용히
# 버려서 **DB 에는 O 로 보이고 봇만 안 뜨는** 상태가 됐다.
#
# 그 상태가 "등록 안 됨" 과 같은 메시지로 나가면 담당자는 토큰을 다시 넣으러 가고,
# 원인인 키 불일치는 그대로 남는다. 조치가 다르면 메시지도 달라야 한다.
def _db_row(key="tyit", **over):
    row = {
        "key": key,
        "label": "전산팀",
        "role": "member",
        "state": "enabled",
        "bot_token": None,
        "app_token": None,
        "readable": [],
    }
    row.update(over)
    return row


def _registry_env(**over):
    env = {"DATABASE_URL": "postgresql://unused", "WORKSPACE_SECRET_KEY": "test-key-present"}
    env.update(over)
    return env


def test_undecryptable_token_blocks_startup_with_its_own_reason(monkeypatch):
    """반쪽만 뜨면 그 본부 대화는 영구 유실된다. 백필은 분당 1회다."""
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store, "runtime_workspaces",
        lambda: [_db_row(secret_error="현재 암호화 키로 복호화 실패")],
    )
    with pytest.raises(ConfigError) as caught:
        load_workspaces(_registry_env())
    message = str(caught.value)
    assert "복호화" in message
    assert "WORKSPACE_SECRET_KEY" in message
    # 잘못된 조치를 안내하지 않는다.
    assert "등록되지 않" not in message


def test_missing_token_keeps_the_registration_message(monkeypatch):
    """사유가 없으면 정말 등록이 안 된 것이다. 그때는 등록을 안내해야 한다."""
    from tybot.console import workspace_store

    monkeypatch.setattr(workspace_store, "runtime_workspaces", lambda: [_db_row()])
    with pytest.raises(ConfigError) as caught:
        load_workspaces(_registry_env())
    assert "등록되지 않" in str(caught.value)
    assert "복호화" not in str(caught.value)


def test_environment_fallback_survives_but_says_so(monkeypatch, caplog):
    """환경변수로 떠 있으면 콘솔에 보이는 값과 실제 사용 값이 다르다."""
    from tybot.console import workspace_store

    monkeypatch.setattr(
        workspace_store, "runtime_workspaces",
        lambda: [_db_row(secret_error="현재 암호화 키로 복호화 실패")],
    )
    env = _registry_env(
        WORKSPACES="tyit",
        SLACK_BOT_TOKEN_TYIT="xoxb-env_1",
        SLACK_APP_TOKEN_TYIT="xapp-env_1",
    )
    with caplog.at_level("WARNING"):
        configs = load_workspaces(env)
    assert [c.key for c in configs] == ["tyit"]
    assert configs[0].bot_token == "xoxb-env_1"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "복호화" in logged
    assert "xoxb-env_1" not in logged  # 토큰은 로그에 남기지 않는다
