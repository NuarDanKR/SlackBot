"""수집 현황 트리.

`상태` 답변이 채널을 워크스페이스 구분 없이 평평하게 나열했다. 경영본부처럼 여러
워크스페이스를 읽는 상위 봇에게는 그 목록이 쓸모가 없다 — 어느 조직의 채널인지 알 수
없고 워크스페이스별 수집이 고른지도 보이지 않는다.

권한도 함께 고친다: 예전에는 아카이브 **전체** 문서를 나열해서, 자기 워크스페이스만
볼 수 있는 봇도 남의 채널명을 보여줬다. 채널명에는 조직·업무가 들어 있다.
"""
from __future__ import annotations

from types import SimpleNamespace

from tybot.status_tree import MAX_CHANNELS, build_tree, render_tree, totals

LABELS = {"mgmt": "경영본부", "pilot": "파일럿", "tyit": "전산팀"}


def doc(ws: str, ch: str, lines: int, last: str = "2026-09-01T10:00+09:00"):
    return SimpleNamespace(
        workspace=ws, channel=ch, raw_lines=[0] * lines, last_ingested=last
    )


DOCS = [
    doc("mgmt", "#본사팀-전산_abb155-공지", 19),
    doc("mgmt", "#본사팀-전산_abb155-생성형ai", 42),
    doc("pilot", "#현장-김해외동_1800249-채팅방", 310),
    doc("secret", "#남의-채널_ABB999-비밀", 999),
]


# --- 권한 --------------------------------------------------------------------
def test_invisible_workspaces_are_dropped():
    """채널명 자체가 조직·업무 노출이다(원칙 3)."""
    nodes = build_tree(DOCS, visible={"mgmt"}, labels=LABELS, own="mgmt")
    assert [n.key for n in nodes] == ["mgmt"]
    rendered = "\n".join(render_tree(nodes))
    assert "남의-채널" not in rendered
    assert "secret" not in rendered


def test_readable_workspaces_are_included():
    nodes = build_tree(DOCS, visible={"mgmt", "pilot"}, labels=LABELS, own="mgmt")
    assert {n.key for n in nodes} == {"mgmt", "pilot"}


def test_member_bot_sees_only_itself():
    nodes = build_tree(DOCS, visible={"pilot"}, labels=LABELS, own="pilot")
    assert [n.key for n in nodes] == ["pilot"]
    assert nodes[0].is_self is True


# --- 구조 --------------------------------------------------------------------
def test_own_workspace_comes_first_even_with_fewer_lines():
    nodes = build_tree(DOCS, visible={"mgmt", "pilot"}, labels=LABELS, own="mgmt")
    assert nodes[0].key == "mgmt"      # 61줄
    assert nodes[1].key == "pilot"     # 310줄


def test_others_are_ordered_by_volume():
    docs = [*DOCS, doc("tyit", "#본사팀-전산_abb110-회의", 500)]
    nodes = build_tree(docs, visible={"mgmt", "pilot", "tyit"}, labels=LABELS, own="mgmt")
    assert [n.key for n in nodes] == ["mgmt", "tyit", "pilot"]


def test_channels_are_ordered_by_lines():
    nodes = build_tree(DOCS, visible={"mgmt"}, labels=LABELS, own="mgmt")
    assert [c.lines for c in nodes[0].channels] == [42, 19]


def test_empty_but_visible_workspace_is_shown():
    """연결은 됐는데 수집이 0인 상태를 사람이 알아채야 한다."""
    nodes = build_tree(DOCS, visible={"mgmt", "tyit"}, labels=LABELS, own="mgmt")
    tyit = next(n for n in nodes if n.key == "tyit")
    assert tyit.docs == 0
    assert "수집된 원문 없음" in "\n".join(render_tree(nodes))


def test_totals_add_up():
    nodes = build_tree(DOCS, visible={"mgmt", "pilot"}, labels=LABELS, own="mgmt")
    t = totals(nodes)
    assert t["문서수"] == 3
    assert t["원문줄수"] == 19 + 42 + 310
    assert t["워크스페이스수"] == 2


def test_totals_carry_only_numbers_for_the_model():
    """LLM 에 문장을 지어낼 여지를 주지 않는다."""
    nodes = build_tree(DOCS, visible={"mgmt"}, labels=LABELS, own="mgmt")
    row = totals(nodes)["워크스페이스별"][0]
    assert set(row) == {"이름", "키", "본인", "문서수", "원문줄수"}


# --- 표시 --------------------------------------------------------------------
def test_render_marks_the_current_workspace():
    nodes = build_tree(DOCS, visible={"mgmt", "pilot"}, labels=LABELS, own="mgmt")
    text = "\n".join(render_tree(nodes))
    assert "*경영본부* (`mgmt`) (이 워크스페이스)" in text
    assert "*파일럿* (`pilot`) —" in text


def test_render_indents_channels_under_the_workspace():
    lines = render_tree(build_tree(DOCS, visible={"mgmt"}, labels=LABELS, own="mgmt"))
    assert lines[0].startswith("*")
    assert lines[1].startswith("    • ")


def test_render_trims_the_timestamp():
    """초·시간대는 상태 답변에서 군더더기다."""
    nodes = build_tree(
        [doc("mgmt", "#c", 1, "2026-08-27T18:02:33+09:00")],
        visible={"mgmt"}, labels=LABELS, own="mgmt",
    )
    assert "(최근 08-27 18:02)" in "\n".join(render_tree(nodes))


def test_missing_timestamp_is_not_an_error():
    nodes = build_tree(
        [doc("mgmt", "#c", 1, "")], visible={"mgmt"}, labels=LABELS, own="mgmt"
    )
    assert "(최근 -)" in "\n".join(render_tree(nodes))


def test_long_workspace_is_folded():
    """상태 답변은 Slack 메시지 하나에 들어가야 한다."""
    docs = [doc("mgmt", f"#c{i}", 100 - i) for i in range(20)]
    lines = render_tree(build_tree(docs, visible={"mgmt"}, labels=LABELS, own="mgmt"))
    bullets = [x for x in lines if x.strip().startswith("•")]
    assert len(bullets) == MAX_CHANNELS
    assert f"그 외 {20 - MAX_CHANNELS}개 채널" in "\n".join(lines)


def test_label_falls_back_to_the_key():
    nodes = build_tree(DOCS, visible={"pilot"}, labels={}, own="pilot")
    assert "*pilot* (`pilot`)" in "\n".join(render_tree(nodes))


def test_nothing_visible_says_so():
    assert "볼 수 있는 워크스페이스가 없습니다" in "\n".join(render_tree([]))


# --- 봇 연결 -----------------------------------------------------------------
def _bot(*, workspace="mgmt", readable=(), docs=DOCS):
    from unittest.mock import Mock

    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = workspace
    bot.cfg = Mock(label=LABELS.get(workspace, workspace), readable=frozenset(readable),
                   is_root=bool(readable))
    bot.store = Mock(docs=lambda: docs, broken=lambda: [])
    bot.workspace_labels = LABELS
    return bot


def test_visible_set_is_self_plus_readable():
    assert _bot(readable=("pilot", "tyit"))._visible_workspaces() == frozenset(
        {"mgmt", "pilot", "tyit"}
    )
    assert _bot()._visible_workspaces() == frozenset({"mgmt"})


def test_bot_tree_uses_the_label_map():
    nodes = _bot(readable=("pilot",))._status_tree()
    assert [n.label for n in nodes] == ["경영본부", "파일럿"]


def test_bot_tree_excludes_workspaces_it_cannot_read():
    keys = {n.key for n in _bot()._status_tree()}
    assert "secret" not in keys
    assert "pilot" not in keys
