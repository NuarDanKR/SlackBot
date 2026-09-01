"""소속 조직 기본값 — `/채널` 에서 조직코드를 외우지 않게 한다.

구분을 고르면 그 층의 내 조직이 조직명·조직코드에 채워진다. **강제하지 않는다** —
채워진 값은 기본값이고, 다른 조직과 협업하는 채널이면 사람이 고쳐 쓴다.
"""
from __future__ import annotations

import pytest

from tybot.channel_management import (
    ChannelNameError,
    _name_inputs,
    create_modal,
    rename_modal,
    request_from_view,
    selected_prefix,
    typed_task,
)
from tybot.channels import PREFIX_ALIASES, parse, should_collect
from tybot.orgsearch import (
    OrgHit,
    defaults_by_prefix,
    derive_prefix,
    encode_value,
    is_site_code,
    my_org_chain,
    search,
    split_org_name,
)


# --- 두문자 이름 변경 ---------------------------------------------------------
# 팀 -> 본사팀, 프로젝트 -> 업무. 이미 만들어진 채널은 계속 수집돼야 한다 -
# 인식을 끊으면 사람은 아무것도 안 했는데 기록이 멈춘다.
@pytest.mark.parametrize(
    "name",
    [
        "#본사팀-전산_ABB110-주간회의",
        "#업무-안전점검_ABB110-협업",
        "#본부-경영_ABB300-워크숍",
        "#실-경영혁신_ABB123-회의",
        "#현장-김해외동_1800249-채팅",
    ],
)
def test_new_prefixes_are_collected(name):
    assert should_collect(name)


@pytest.mark.parametrize("name", ["#팀-전산_ABB110-주간회의", "#프로젝트-스마트시티_ABB110-회의"])
def test_legacy_prefixes_keep_working(name):
    """옛 이름으로 만들어진 채널의 수집이 멈추면 안 된다."""
    assert should_collect(name)


def test_longer_prefix_is_matched_first():
    """`본사팀` 이 `팀` 보다 뒤에 오면 영영 매치되지 않는다."""
    spec = parse("#본사팀-전산_ABB110-회의")
    assert spec.prefix == "본사팀"
    assert spec.org_name == "전산"


def test_legacy_prefix_maps_to_the_new_one():
    assert PREFIX_ALIASES["팀"] == "본사팀"
    assert PREFIX_ALIASES["프로젝트"] == "업무"


def test_kind_is_stable_across_the_rename():
    """조직 트리 연결에 쓰는 값은 이름을 바꿔도 같아야 한다."""
    assert parse("#팀-전산_ABB110-회의").kind == parse("#본사팀-전산_ABB110-회의").kind
    assert parse("#프로젝트-x_ABB110-y").kind == parse("#업무-x_ABB110-y").kind


# --- 조직명에서 구분 뽑기 ----------------------------------------------------
@pytest.mark.parametrize(
    ("name", "suffix", "base"),
    [
        ("경영혁신실", "실", "경영혁신"),
        ("경영본부", "본부", "경영"),
        ("전산팀", "팀", "전산"),
        ("김해외동현장", "현장", "김해외동"),
        ("경영지원", "", "경영지원"),
        ("팀", "", "팀"),
    ],
)
def test_split_org_name(name, suffix, base):
    assert split_org_name(name) == (suffix, base)


@pytest.mark.parametrize(
    ("code", "site"),
    [
        ("ABB110", False),
        ("1800249", True),
        ("180182", True),
        ("A1800249", False),
        ("", False),
    ],
)
def test_is_site_code(code, site):
    """본사는 코드에 알파벳이 있고 현장은 숫자뿐이다."""
    assert is_site_code(code) is site


@pytest.mark.parametrize(
    ("code", "name", "prefix"),
    [
        ("ABB123", "경영혁신실", "실"),
        ("ABB300", "경영본부", "본부"),
        ("ABB110", "전산팀", "본사팀"),      # 팀 -> 본사팀
        ("1800249", "김해외동", "현장"),      # 접미사가 없어도 코드가 현장
        ("180182", "공무팀", "현장"),         # 현장 소속 팀 - 코드가 이긴다
        ("ABB999", "경영지원", ""),           # 본사인데 접미사 없음 -> 사용자 선택
        ("ABB800", "안전현장", ""),           # 본사 코드에 현장을 붙이지 않는다
    ],
)
def test_derive_prefix(code, name, prefix):
    assert derive_prefix(code, name) == prefix


def test_base_name_keeps_suffix_that_was_not_used():
    """구분으로 쓰지 않은 접미사를 떼면 무슨 조직인지 알 수 없게 된다."""
    assert OrgHit("180182", "공무팀").base_name == "공무팀"
    assert OrgHit("ABB800", "안전현장").base_name == "안전현장"


def test_base_name_strips_the_suffix_that_was_used():
    assert OrgHit("ABB123", "경영혁신실").base_name == "경영혁신"
    assert OrgHit("ABB110", "전산팀").base_name == "전산"


# --- 소속 사슬 ----------------------------------------------------------------
class FakeCursor:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.c.executed.append((sql, params))

    def fetchall(self):
        return self.c.rows


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed: list[tuple] = []

    def cursor(self):
        return FakeCursor(self)


CHAIN_ROWS = [
    {"code": "ABB110", "name": "전산팀", "depth": 1},
    {"code": "ABB300", "name": "경영본부", "depth": 2},
]


def test_chain_walks_up_from_my_org():
    conn = FakeConn(CHAIN_ROWS)
    chain = my_org_chain(conn, workspace="pilot", slack_user="U1")
    assert [h.code for h in chain] == ["ABB110", "ABB300"]
    sql, params = conn.executed[-1]
    assert "with recursive" in sql
    assert params == {"workspace": "pilot", "slack_user": "U1"}


def test_chain_stops_recursing_on_a_cycle():
    """스키마는 자기 자신이 부모인 것만 막는다. 더 긴 순환은 여기서 끊는다."""
    from tybot.orgsearch import MY_ORGS_SQL

    assert "up.depth < 20" in MY_ORGS_SQL


def test_chain_only_counts_active_rows():
    from tybot.orgsearch import MY_ORGS_SQL

    assert "e.active" in MY_ORGS_SQL
    assert "o.active" in MY_ORGS_SQL
    assert "p.active" in MY_ORGS_SQL


def test_chain_is_empty_without_identity():
    assert my_org_chain(FakeConn([]), workspace="pilot", slack_user="U1") == []
    assert my_org_chain(FakeConn(), workspace="pilot", slack_user="") == []


def test_chain_accepts_tuple_cursor():
    conn = FakeConn([("ABB110", "전산팀", 1)])
    assert my_org_chain(conn, workspace="pilot", slack_user="U1")[0].name == "전산팀"


def test_search_reads_active_orgs_by_name_or_code():
    conn = FakeConn([{"code": "ABB110", "name": "전산팀", "parent_name": "경영본부"}])
    hits = search(conn, "전산")
    assert hits == [OrgHit("ABB110", "전산팀", "경영본부")]
    sql, params = conn.executed[-1]
    assert "where o.active" in sql
    assert params["like"] == "%전산%"


# --- 구분별 기본값 ------------------------------------------------------------
def _chain() -> list[OrgHit]:
    return [OrgHit("ABB110", "전산팀"), OrgHit("ABB300", "경영본부")]


def test_defaults_map_each_level():
    d = defaults_by_prefix(_chain())
    assert d["본사팀"].code == "ABB110"
    assert d["본부"].code == "ABB300"


def test_task_channel_borrows_the_owning_team():
    """업무는 조직이 아니다. 주관 팀의 코드를 그대로 쓴다."""
    d = defaults_by_prefix(_chain())
    assert d["업무"].code == "ABB110"


def test_nearest_org_wins_when_a_level_repeats():
    chain = [OrgHit("ABB110", "전산팀"), OrgHit("ABB300", "경영본부"), OrgHit("ABB900", "지주본부")]
    assert defaults_by_prefix(chain)["본부"].code == "ABB300"


def test_site_member_gets_a_site_default():
    d = defaults_by_prefix([OrgHit("1800249", "김해외동"), OrgHit("ABB300", "경영본부")])
    assert d["현장"].code == "1800249"
    assert d["업무"].code == "1800249"  # 주관 조직이 현장이면 현장 코드를 빌린다


def test_levels_without_a_match_are_absent():
    """없는 층은 비워 둔다. 엉뚱한 조직을 채우면 사람이 그대로 만들어 버린다."""
    assert "실" not in defaults_by_prefix(_chain())


# --- 모달 ---------------------------------------------------------------------
def test_modal_searches_org_and_hides_the_code_input():
    ids = [b["block_id"] for b in _name_inputs()]
    assert ids == ["prefix", "org", "task"]
    assert _name_inputs()[1]["element"]["type"] == "external_select"
    assert "org_code" not in ids


def test_prefix_select_dispatches_so_defaults_can_be_filled():
    """dispatch_action 이 없으면 선택 사실이 서버에 오지 않아 자동 채움이 안 된다."""
    assert _name_inputs()[0]["dispatch_action"] is True


def test_rename_prefix_does_not_open_the_create_modal_flow():
    spec = parse("#본사팀-전산_ABB110-회의")
    assert spec is not None
    modal = rename_modal("{}", spec)
    prefix = next(b for b in modal["blocks"] if b["block_id"] == "prefix")
    assert prefix["dispatch_action"] is False


def test_prefix_options_use_the_new_names():
    options = _name_inputs()[0]["element"]["options"]
    assert [o["value"] for o in options] == ["본부", "실", "본사팀", "현장", "업무"]


def test_hint_explains_the_structure_and_task_channels():
    hint = _name_inputs()[0]["hint"]["text"]
    assert "본부 > 본사팀 > 현장" in hint
    assert "다른 팀과 협업" in hint


def test_defaults_select_the_matching_org():
    blocks = _name_inputs(prefix="본사팀", defaults=defaults_by_prefix(_chain()))
    assert blocks[1]["element"]["initial_option"]["value"] == "ABB110|본사팀|전산"


def test_switching_prefix_switches_the_default():
    blocks = _name_inputs(prefix="본부", defaults=defaults_by_prefix(_chain()))
    assert blocks[1]["element"]["initial_option"]["value"] == "ABB300|본부|경영"


def test_other_orgs_are_chosen_by_search_not_by_typing_a_code():
    blocks = _name_inputs(prefix="본사팀", defaults=defaults_by_prefix(_chain()))
    assert blocks[1]["element"]["type"] == "external_select"
    assert "자동" in blocks[1]["hint"]["text"]


def test_no_defaults_leaves_the_fields_empty():
    blocks = _name_inputs()
    assert "initial_option" not in blocks[1]["element"]


def test_legacy_prefix_is_mapped_when_reopening():
    """이름 변경 모달이 옛 두문자를 주면 목록에 없는 값이 된다."""
    picked = _name_inputs(prefix="팀")[0]["element"]["initial_option"]["value"]
    assert picked == "본사팀"


def test_create_modal_keeps_typed_task_when_redrawn():
    """다시 그렸다고 입력이 사라지면 안 된다."""
    view = create_modal("{}", prefix="본부", defaults=defaults_by_prefix(_chain()), task="워크숍")
    task_block = next(b for b in view["blocks"] if b.get("block_id") == "task")
    assert task_block["element"]["initial_value"] == "워크숍"


# --- 다시 그리기에 필요한 값 읽기 ---------------------------------------------
def _view(**blocks) -> dict:
    return {"state": {"values": blocks}}


def _prefix(p: str) -> dict:
    return {"prefix": {"prefix": {"selected_option": {"value": p}}}}


def _text(block: str, value: str) -> dict:
    return {block: {block: {"value": value}}}


def _org(code: str, name: str) -> dict:
    return {
        "org": {
            "org": {"selected_option": {"value": encode_value(OrgHit(code, name))}}
        }
    }


def test_selected_prefix_reads_the_current_choice():
    assert selected_prefix(_view(**_prefix("본부"))) == "본부"


def test_selected_prefix_falls_back_to_the_default():
    assert selected_prefix(_view()) == "본사팀"


def test_typed_task_survives_a_redraw():
    assert typed_task(_view(**_text("task", "주간회의"))) == "주간회의"
    assert typed_task(_view()) == ""


# --- 제출 ---------------------------------------------------------------------
def test_submit_builds_the_channel_name():
    view = _view(
        **_prefix("본사팀"),
        **_org("ABB110", "전산팀"),
        **_text("task", "주간회의"),
    )
    assert request_from_view(view, include_channel_options=False).name == (
        "팀-전산_ABB110-주간회의"
    )


def test_task_channel_uses_the_owning_team_code():
    """업무 채널: 협업 이름 + 주관 팀 코드."""
    view = _view(
        **_prefix("업무"),
        **_org("ABB110", "전산팀"),
        **_text("task", "협업"),
    )
    assert request_from_view(view, include_channel_options=False).name == (
        "업무-전산_ABB110-협업"
    )


def test_other_org_is_allowed():
    """소속을 강제하지 않는다. 검색에서 다른 조직을 고를 수 있다."""
    view = _view(
        **_prefix("본사팀"),
        **_org("ABB540", "자금팀"),
        **_text("task", "협업"),
    )
    assert request_from_view(view, include_channel_options=False).name == (
        "팀-자금_ABB540-협업"
    )


def test_legacy_prefix_cannot_be_submitted():
    """새로 만들 때는 새 이름만 쓴다. 인식만 옛 이름을 받아준다."""
    view = _view(
        **_prefix("팀"), **_org("ABB110", "전산팀"), **_text("task", "회의"),
    )
    with pytest.raises(ChannelNameError) as e:
        request_from_view(view, include_channel_options=False)
    assert e.value.block_id == "prefix"


def test_missing_org_selection_is_refused_on_the_search_block():
    view = _view(
        **_prefix("본사팀"), **_text("task", "회의"),
    )
    with pytest.raises(ChannelNameError) as e:
        request_from_view(view, include_channel_options=False)
    assert e.value.block_id == "org"


# --- 봇 쪽 ---------------------------------------------------------------------
def _bot():
    from unittest.mock import Mock

    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "pilot"
    bot.app = Mock()
    return bot


def test_defaults_are_empty_without_db(monkeypatch):
    """채널 생성이 DB 가용성에 묶이면 안 된다."""
    import contextlib

    import tybot.slack.pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(None))
    assert _bot()._org_defaults("U1") == {}


def test_defaults_survive_a_query_failure(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    class Boom:
        def cursor(self):
            raise RuntimeError("relation org_unit does not exist")

    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(Boom()))
    assert _bot()._org_defaults("U1") == {}


def test_defaults_come_from_the_chain(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    monkeypatch.setattr(
        pilot_mod, "db_connect", lambda: contextlib.nullcontext(FakeConn(CHAIN_ROWS))
    )
    d = _bot()._org_defaults("U1")
    assert d["본사팀"].code == "ABB110"
    assert d["본부"].name == "경영본부"


def test_org_search_filters_results_to_the_selected_prefix(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    rows = [
        {"code": "ABB110", "name": "전산팀", "parent_name": "경영본부"},
        {"code": "ABB300", "name": "경영본부", "parent_name": ""},
    ]
    monkeypatch.setattr(
        pilot_mod, "db_connect", lambda: contextlib.nullcontext(FakeConn(rows))
    )
    found = _bot()._org_options("경영", prefix="본사팀")
    assert [item["value"] for item in found] == ["ABB110|본사팀|전산"]
