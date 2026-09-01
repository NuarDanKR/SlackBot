"""조직 검색 — `/채널` 에서 조직코드를 외우지 않게 한다.

`ABB110` 같은 코드를 사람이 직접 적으면, 틀려도 채널은 만들어지고 조직 매핑만 어긋난다.
그건 나중에 권한·집계에서야 드러난다. 목록에서 고르게 하고, 구분(본부/실/팀)은
**조직명 끝**에서 뽑는다 — `org_unit.kind` 는 그룹웨어에 구분 컬럼이 없어 추정한 값이라
채널명의 근거로 쓰지 않는다.
"""
from __future__ import annotations

import pytest

from tybot.channel_management import (
    ChannelNameError,
    _name_inputs,
    create_modal,
    request_from_view,
)
from tybot.orgsearch import (
    MAX_TEXT,
    NO_MATCH,
    OrgHit,
    decode_value,
    derive_prefix,
    encode_value,
    is_site_code,
    notice_option,
    option,
    options,
    search,
    split_org_name,
)


# --- 조직명에서 구분 뽑기 ----------------------------------------------------
@pytest.mark.parametrize(
    ("name", "prefix", "base"),
    [
        ("경영혁신실", "실", "경영혁신"),
        ("경영본부", "본부", "경영"),
        ("전산팀", "팀", "전산"),
        ("김해외동현장", "현장", "김해외동"),
        ("스마트시티프로젝트", "프로젝트", "스마트시티"),
        ("경영지원", "", "경영지원"),   # 끝이 규칙에 없다
        ("팀", "", "팀"),               # 구분어뿐이면 자르지 않는다
    ],
)
def test_split_org_name(name, prefix, base):
    assert split_org_name(name) == (prefix, base)


def test_hit_exposes_prefix_and_base():
    hit = OrgHit(code="ABB123", name="경영혁신실")
    assert hit.prefix == "실"
    assert hit.base_name == "경영혁신"


# --- 옵션 --------------------------------------------------------------------
def test_option_shows_parent_to_disambiguate():
    """'경영' 으로 여러 조직이 나올 때 상위 조직이 없으면 무엇을 고를지 알 수 없다."""
    text = option(OrgHit("ABB123", "경영혁신실", "태영건설"))["text"]["text"]
    assert "경영혁신실" in text
    assert "태영건설" in text
    assert "ABB123" in text


def test_option_text_fits_slack_limit():
    long = OrgHit("A" * 20, "가" * 60, "나" * 60)
    assert len(option(long)["text"]["text"]) <= MAX_TEXT


def test_value_round_trips():
    hit = OrgHit("ABB123", "경영혁신실")
    assert decode_value(encode_value(hit)) == ("ABB123", "실", "경영혁신")


def test_value_keeps_code_first_so_truncation_is_survivable():
    """value 는 75자 제한이 있다. 잘리더라도 코드는 남아야 한다."""
    hit = OrgHit("ABB123", "가" * 90)
    assert option(hit)["value"].startswith("ABB123")


def test_decode_ignores_unknown_prefix():
    assert decode_value("ABB123|없는구분|이름") == ("ABB123", "", "이름")


def test_decode_handles_garbage():
    assert decode_value("") == ("", "", "")
    assert decode_value("ABB123") == ("ABB123", "", "")


def test_notice_option_has_empty_value():
    """안내 항목을 고르면 제출 단계에서 막혀야 한다."""
    assert notice_option(NO_MATCH)["value"] == ""


# --- 검색 SQL ----------------------------------------------------------------
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


def test_search_matches_name_and_code():
    conn = FakeConn([{"code": "ABB123", "name": "경영혁신실", "parent_name": "태영건설"}])
    hits = search(conn, "경영")
    assert hits == [OrgHit("ABB123", "경영혁신실", "태영건설")]
    sql, params = conn.executed[-1]
    assert "o.name ilike %(like)s or o.code ilike %(prefix)s" in sql
    assert params["like"] == "%경영%"
    assert params["prefix"] == "경영%"


def test_search_excludes_inactive_orgs():
    """폐지 조직으로 채널을 만들면 처음부터 잘못된 조직에 매달린다."""
    conn = FakeConn()
    search(conn, "경영")
    assert "o.active" in conn.executed[-1][0]


def test_search_orders_prefix_matches_first():
    """'경영' 을 치면 '경영본부' 가 '정보경영팀' 보다 위에 와야 한다."""
    conn = FakeConn()
    search(conn, "경영")
    sql = conn.executed[-1][0]
    assert "case when o.name ilike %(prefix)s then 0 else 1 end" in sql
    assert "length(o.name)" in sql


def test_search_can_limit_to_one_company():
    conn = FakeConn()
    search(conn, "경영", company_code="TY")
    assert conn.executed[-1][1]["company"] == "TY"
    assert "o.company_code = %(company)s" in conn.executed[-1][0]


def test_empty_query_does_not_hit_the_db():
    conn = FakeConn()
    assert search(conn, "   ") == []
    assert conn.executed == []


def test_search_accepts_tuple_cursor():
    conn = FakeConn([("ABB123", "경영혁신실", "태영건설")])
    assert search(conn, "경영")[0].code == "ABB123"


def test_option_count_is_capped():
    hits = [OrgHit(f"C{i}", f"조직{i}팀") for i in range(50)]
    assert len(options(hits)) <= 25


# --- 모달 --------------------------------------------------------------------
def test_search_modal_replaces_the_two_manual_inputs():
    blocks = _name_inputs(org_search=True)
    ids = [b["block_id"] for b in blocks]
    assert ids == ["prefix", "org", "project_name", "task"]
    assert blocks[1]["element"]["type"] == "external_select"


def test_manual_modal_is_kept_when_db_is_unavailable():
    """채널 생성이 DB 가용성에 묶이면 안 된다."""
    ids = [b["block_id"] for b in _name_inputs(org_search=False)]
    assert ids == ["prefix", "org_name", "org_code", "task"]


def test_prefix_label_explains_when_it_is_used():
    """검색으로 고르면 이 선택이 무시된다 - 안 적으면 '왜 반영이 안 되지' 가 된다."""
    label = _name_inputs(org_search=True)[0]["label"]["text"]
    assert "프로젝트는 직접 선택" in label
    assert "자동 판단" in label


def test_org_block_states_the_code_rule():
    """구분이 무엇으로 정해지는지 화면에서 알 수 있어야 한다."""
    hint = _name_inputs(org_search=True)[1]["hint"]["text"]
    assert "숫자만이면 현장" in hint
    assert "알파벳" in hint


def test_create_modal_passes_the_flag():
    ids = [b.get("block_id") for b in create_modal("{}", org_search=True)["blocks"]]
    assert "org" in ids
    assert "org_code" not in ids


# --- 제출 --------------------------------------------------------------------
def _view(**blocks) -> dict:
    return {"state": {"values": blocks}}


def _picked(value: str) -> dict:
    return {"org": {"org": {"selected_option": {"value": value, "text": {"text": "x"}}}}}


def _prefix(p: str) -> dict:
    return {"prefix": {"prefix": {"selected_option": {"value": p}}}}


def _task(v: str) -> dict:
    return {"task": {"task": {"value": v}}}


def test_picked_org_builds_the_channel_name():
    """사용자가 요청한 동작: '경영혁신실' 을 고르면 실-경영혁신_코드-업무."""
    view = _view(**_prefix("팀"), **_picked("ABB123|실|경영혁신"), **_task("주간회의"))
    req = request_from_view(view, include_channel_options=False)
    assert req.name == "실-경영혁신_ABB123-주간회의"


def test_name_suffix_wins_over_the_prefix_select():
    """조직명은 사람이 붙인 것이고, kind 는 추정값이다. 이름 쪽을 믿는다."""
    view = _view(**_prefix("현장"), **_picked("ABB123|본부|경영"), **_task("회의"))
    assert request_from_view(view, include_channel_options=False).prefix == "본부"


def test_prefix_select_is_used_when_the_name_has_no_suffix():
    view = _view(**_prefix("팀"), **_picked("ABB999||경영지원"), **_task("회의"))
    assert request_from_view(view, include_channel_options=False).name == (
        "팀-경영지원_ABB999-회의"
    )


def test_notice_option_is_refused_at_submit():
    view = _view(**_prefix("팀"), **_picked(""), **_task("회의"))
    with pytest.raises(ChannelNameError) as e:
        request_from_view(view, include_channel_options=False)
    assert e.value.block_id == "org"


def test_manual_path_still_works():
    view = _view(
        **_prefix("팀"),
        org_name={"org_name": {"value": "전산"}},
        org_code={"org_code": {"value": "abb110"}},
        **_task("주간회의"),
    )
    req = request_from_view(view, include_channel_options=False)
    assert req.name == "팀-전산_ABB110-주간회의"


# --- 검색 콜백 ---------------------------------------------------------------
def _bot():
    from unittest.mock import Mock

    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "pilot"
    bot.app = Mock()
    bot.org_scope = False          # 소속 제한 없는 기본 상황
    bot.org_scope_strict = False
    bot.channel_admin_users = set()
    return bot


def test_callback_returns_options(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    conn = FakeConn([{"code": "ABB123", "name": "경영혁신실", "parent_name": "태영건설"}])
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(conn))
    opts = _bot()._org_options("경영")
    assert opts[0]["value"].startswith("ABB123")


def test_callback_says_so_when_db_is_down(monkeypatch):
    """빈 목록은 '고장' 과 '결과 없음' 을 구별해 주지 않는다."""
    import tybot.slack.pilot as pilot_mod

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: __import__("contextlib").nullcontext(None))
    (opt,) = _bot()._org_options("경영")
    assert "조회할 수 없습니다" in opt["text"]["text"]
    assert opt["value"] == ""


def test_callback_survives_query_failure(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    class Boom:
        def cursor(self):
            raise RuntimeError("relation org_unit does not exist")

    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(Boom()))
    (opt,) = _bot()._org_options("경영")
    assert opt["value"] == ""


def test_callback_reports_no_match(monkeypatch):
    import contextlib

    import tybot.slack.pilot as pilot_mod

    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(FakeConn()))
    (opt,) = _bot()._org_options("없는조직")
    assert opt["text"]["text"] == NO_MATCH


def test_callback_does_not_log_full_query(monkeypatch, caplog):
    """검색어에 사람 이름이 들어올 수 있다. 길게 남기지 않는다."""
    import contextlib

    import tybot.slack.pilot as pilot_mod

    conn = FakeConn([{"code": "A", "name": "경영팀", "parent_name": ""}])
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(conn))
    with caplog.at_level("INFO"):
        _bot()._org_options("가" * 100)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "가" * 100 not in logged


# --- 조직코드로 본사·현장 가르기 ---------------------------------------------
# 본사 조직은 코드에 알파벳이 있다(ABB110). 현장은 숫자만이다(1800249).
# 이름은 사람이 바꿀 수 있지만 코드 체계는 그룹웨어가 준다.
@pytest.mark.parametrize(
    ("code", "site"),
    [
        ("ABB110", False),
        ("ABB123", False),
        ("1800249", True),
        ("180182", True),
        ("A1800249", False),   # 알파벳이 하나라도 있으면 본사
        ("", False),           # 코드가 없으면 판단하지 않는다
        ("  ", False),
    ],
)
def test_is_site_code(code, site):
    assert is_site_code(code) is site


@pytest.mark.parametrize(
    ("code", "name", "prefix"),
    [
        ("ABB123", "경영혁신실", "실"),
        ("ABB300", "경영본부", "본부"),
        ("ABB110", "전산팀", "팀"),
        ("1800249", "김해외동", "현장"),        # 이름에 접미사가 없어도 코드가 현장
        ("1800249", "김해외동현장", "현장"),
        ("180182", "공무팀", "현장"),           # 현장 소속 팀 - 코드가 이긴다
        ("ABB999", "경영지원", ""),             # 본사인데 접미사 없음 -> 사용자 선택
        ("ABB800", "안전현장", ""),             # 본사 코드에 현장을 붙이지 않는다
        ("ABB700", "스마트시티프로젝트", "프로젝트"),  # 프로젝트는 본사/현장 축 밖
        ("1800300", "재개발프로젝트", "프로젝트"),
    ],
)
def test_derive_prefix(code, name, prefix):
    assert derive_prefix(code, name) == prefix


def test_site_code_wins_over_name_suffix():
    """현장 조직의 '공무팀' 이 팀으로 분류되면 채널명만으로 현장을 구분할 수 없다."""
    assert OrgHit("180182", "공무팀").prefix == "현장"


def test_hq_code_never_becomes_a_site():
    """본사 조직에 현장이 붙으면 조직 집계가 어긋난다."""
    assert OrgHit("ABB800", "안전현장").prefix == ""


def test_base_name_keeps_suffix_that_was_not_used():
    """구분으로 쓰지 않은 접미사를 떼면 무슨 조직인지 알 수 없게 된다."""
    assert OrgHit("180182", "공무팀").base_name == "공무팀"
    assert OrgHit("ABB800", "안전현장").base_name == "안전현장"


def test_base_name_strips_the_suffix_that_was_used():
    assert OrgHit("ABB123", "경영혁신실").base_name == "경영혁신"
    assert OrgHit("1800249", "김해외동현장").base_name == "김해외동"


def test_site_without_suffix_keeps_its_name():
    assert OrgHit("1800249", "김해외동").base_name == "김해외동"


def test_option_label_shows_the_derived_prefix():
    """고르기 전에 어떤 채널명이 될지 알 수 있어야 한다."""
    assert option(OrgHit("1800249", "김해외동"))["text"]["text"].startswith("[현장]")
    assert option(OrgHit("ABB999", "경영지원"))["text"]["text"].startswith("경영지원")


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("ABB123", "경영혁신실", "실-경영혁신_ABB123-주간회의"),
        ("1800249", "김해외동", "현장-김해외동_1800249-주간회의"),
        ("180182", "공무팀", "현장-공무팀_180182-주간회의"),
    ],
)
def test_end_to_end_channel_name(code, name, expected):
    from tybot.channel_management import build_channel_name

    hit = OrgHit(code, name)
    view = _view(**_prefix("팀"), **_picked(encode_value(hit)), **_task("주간회의"))
    req = request_from_view(view, include_channel_options=False)
    assert req.name == expected
    assert build_channel_name(hit.prefix, hit.base_name, hit.code, "주간회의") == expected


# --- 소속 조직 제한 -----------------------------------------------------------
# 전산팀원이면 전산팀·경영본부 채널만 만들 수 있다. 남의 팀 채널을 내가 만들면
# 그 채널의 주인이 처음부터 어긋난다.
def test_my_orgs_walks_up_the_tree():
    from tybot.orgsearch import MY_ORGS_SQL, my_org_codes

    conn = FakeConn([{"code": "ABB110"}, {"code": "ABB300"}])
    codes = my_org_codes(conn, workspace="pilot", slack_user="U1")
    assert codes == ["ABB110", "ABB300"]
    _, params = conn.executed[-1]
    assert "with recursive" in MY_ORGS_SQL
    assert params == {"workspace": "pilot", "slack_user": "U1"}


def test_my_orgs_stops_recursing_on_a_cycle():
    """스키마는 자기 자신이 부모인 것만 막는다. 더 긴 순환은 여기서 끊는다."""
    from tybot.orgsearch import MY_ORGS_SQL

    assert "up.depth < 20" in MY_ORGS_SQL


def test_my_orgs_returns_none_without_identity():
    """'모른다' 와 '소속이 없다' 는 다르게 다뤄야 한다."""
    from tybot.orgsearch import my_org_codes

    assert my_org_codes(FakeConn([]), workspace="pilot", slack_user="U1") is None
    assert my_org_codes(FakeConn(), workspace="pilot", slack_user="") is None


def test_my_orgs_only_counts_active_rows():
    from tybot.orgsearch import MY_ORGS_SQL

    assert "e.active" in MY_ORGS_SQL
    assert "o.active" in MY_ORGS_SQL
    assert "p.active" in MY_ORGS_SQL


def test_search_can_be_limited_to_my_orgs():
    conn = FakeConn()
    search(conn, "경영", only_codes=["ABB110", "ABB300"])
    sql, params = conn.executed[-1]
    assert "o.code = any(%(only)s)" in sql
    assert params["only"] == ["ABB110", "ABB300"]


def test_empty_allowed_list_finds_nothing_without_querying():
    """'소속이 하나도 없다' 를 '제한 없음' 으로 바꿔 읽으면 전 조직이 열린다."""
    conn = FakeConn([{"code": "X", "name": "아무개팀", "parent_name": ""}])
    assert search(conn, "경영", only_codes=[]) == []
    assert conn.executed == []


def test_no_limit_when_only_codes_is_none():
    conn = FakeConn()
    search(conn, "경영", only_codes=None)
    assert conn.executed[-1][1]["only"] is None


# --- 봇 쪽 판정 ---------------------------------------------------------------
def _scoped_bot(*, allowed=None, strict=False, admins=()):
    from unittest.mock import Mock

    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "pilot"
    bot.app = Mock()
    bot.org_scope = True
    bot.org_scope_strict = strict
    bot.channel_admin_users = set(admins)
    bot._my_org_codes = lambda user_id: allowed
    return bot


def test_own_org_passes():
    assert _scoped_bot(allowed=["ABB110", "ABB300"])._org_scope_error(
        "U1", "ABB110", "전산팀"
    ) == ""


def test_parent_org_passes():
    """팀원이 본부 단위 채널을 만드는 일이 실제로 있다."""
    assert _scoped_bot(allowed=["ABB110", "ABB300"])._org_scope_error(
        "U1", "ABB300", "경영본부"
    ) == ""


def test_other_org_is_refused_with_a_reason():
    msg = _scoped_bot(allowed=["ABB110"])._org_scope_error("U1", "ABB540", "자금팀")
    assert "자금팀" in msg
    assert "소속 조직이 아닙니다" in msg


def test_unknown_identity_passes_by_default():
    """신원 매핑 작업이 끝나기 전에 채널 생성이 통째로 멈추면 안 된다."""
    assert _scoped_bot(allowed=None)._org_scope_error("U1", "ABB540", "자금팀") == ""


def test_unknown_identity_is_refused_in_strict_mode():
    msg = _scoped_bot(allowed=None, strict=True)._org_scope_error("U1", "ABB540", "자금팀")
    assert "계정 연결" in msg


def test_channel_admin_bypasses_the_scope():
    bot = _scoped_bot(allowed=["ABB110"], admins={"U9"})
    assert bot._org_scope_error("U9", "ABB540", "자금팀") == ""


def test_scope_off_allows_everything():
    bot = _scoped_bot(allowed=["ABB110"])
    bot.org_scope = False
    assert bot._org_scope_error("U1", "ABB540", "자금팀") == ""


def test_options_say_why_the_list_is_empty(monkeypatch):
    """소속 제한으로 0건이면 '없다' 가 아니라 '소속만 된다' 를 알려야 한다."""
    import contextlib

    import tybot.slack.pilot as pilot_mod

    bot = _scoped_bot(allowed=["ABB110"])
    monkeypatch.setattr(pilot_mod, "db_connect", lambda: contextlib.nullcontext(FakeConn()))
    (opt,) = bot._org_options("자금", "U1")
    assert "소속 조직만" in opt["text"]["text"]


# --- 프로젝트: 주관 조직의 코드를 빌린다 --------------------------------------
# 프로젝트는 정식 조직이 아니라 조직코드가 없다. 채널명 규칙은 코드를 요구하므로
# 주관 조직의 코드를 쓰고, 이름 자리에는 프로젝트명을 넣는다.
def _project(name: str) -> dict:
    return {"project_name": {"project_name": {"value": name}}}


def test_project_borrows_the_owning_org_code():
    view = _view(
        **_prefix("프로젝트"),
        **_picked("ABB110|팀|전산"),
        **_project("스마트시티"),
        **_task("주간회의"),
    )
    req = request_from_view(view, include_channel_options=False)
    assert req.name == "프로젝트-스마트시티_ABB110-주간회의"


def test_project_selection_overrides_the_derived_prefix():
    """주관 조직이 전산팀이어도 사용자가 프로젝트를 고르면 프로젝트다."""
    view = _view(
        **_prefix("프로젝트"),
        **_picked("ABB110|팀|전산"),
        **_project("스마트시티"),
        **_task("회의"),
    )
    assert request_from_view(view, include_channel_options=False).prefix == "프로젝트"


def test_project_without_a_name_is_refused_on_that_block():
    view = _view(
        **_prefix("프로젝트"), **_picked("ABB110|팀|전산"), **_project("  "), **_task("회의")
    )
    with pytest.raises(ChannelNameError) as e:
        request_from_view(view, include_channel_options=False)
    assert e.value.block_id == "project_name"
    assert "주관하는" in str(e.value)


def test_project_name_is_ignored_when_prefix_is_not_project():
    """프로젝트명을 적어두고 구분을 바꿔도 조직명이 오염되지 않는다."""
    view = _view(
        **_prefix("팀"),
        **_picked("ABB110|팀|전산"),
        **_project("스마트시티"),
        **_task("회의"),
    )
    assert request_from_view(view, include_channel_options=False).name == (
        "팀-전산_ABB110-회의"
    )


def test_project_block_is_optional_in_the_modal():
    """구분이 프로젝트일 때만 쓰이므로 평소에는 비워 둘 수 있어야 한다."""
    block = next(
        b for b in _name_inputs(org_search=True) if b["block_id"] == "project_name"
    )
    assert block["optional"] is True
    assert "조직코드가 없어" in block["hint"]["text"]


def test_manual_mode_keeps_typed_org_name_for_projects():
    """DB 없는 환경에는 프로젝트명 칸이 없다. 조직명 칸에 적은 값을 그대로 쓴다."""
    view = _view(
        **_prefix("프로젝트"),
        org_name={"org_name": {"value": "스마트시티"}},
        org_code={"org_code": {"value": "ABB110"}},
        **_task("회의"),
    )
    assert request_from_view(view, include_channel_options=False).name == (
        "프로젝트-스마트시티_ABB110-회의"
    )


def test_project_scope_still_checks_the_owning_org():
    """빌린 코드가 소속 밖이면 여전히 막힌다 - 프로젝트가 우회로가 되면 안 된다."""
    bot = _scoped_bot(allowed=["ABB110"])
    assert bot._org_scope_error("U1", "ABB540", "자금팀") != ""
    assert bot._org_scope_error("U1", "ABB110", "전산팀") == ""
