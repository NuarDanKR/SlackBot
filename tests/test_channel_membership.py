"""초대 기반 채널 발견 — **초대가 정본이다.**

결정: 2026-09-29 오너 지시.

손으로 적던 목록이 못 막던 두 가지를 여기서 고정한다.

1. 초대했는데 등록을 잊어 **수집이 안 되는 채널**
2. 초대를 풀었는데 목록에 남아 **권한 오류만 쌓는 채널**

그리고 새로 생기는 위험도 함께 막는다 — 앱 설치만으로 전 채널을 긁는 것,
DM 이 섞여 들어오는 것, 재초대가 사람이 끈 채널까지 되살리는 것.
"""

from __future__ import annotations

import pytest
from fake_archiving_repo import SETTLE_SECONDS, FakeArchivingRepo

from tybot.archive import channel_membership as membership
from tybot.archive.channel_membership import Channel


class FakeSlack:
    """`users.conversations` 만 흉내 낸다. **페이지를 나눠 준다.**"""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def users_conversations(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


class FakeRepo:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = {str(row["channel_id"]): dict(row) for row in (rows or [])}
        self.audit: list[dict] = []

    def channels(self, workspace: str) -> list[dict]:
        return [dict(row) for row in self.rows.values()]

    def save_membership(self, row: dict) -> None:
        current = self.rows.setdefault(row["channel_id"], {})
        current.update(row)
        if row.get("audit"):
            self.audit.append({
                "actor": row["updated_by"],
                "subject": "channel_membership",
                "workspace": row["workspace"],
                "channel_id": row["channel_id"],
                "field": "membership",
                "old_value": "",
                "new_value": f"{row['membership']}/{row['mode']}",
                "reason": row["reason"],
            })

    def add_audit(self, row: dict) -> None:
        self.audit.append(dict(row))


def _page(channels, cursor=""):
    return {
        "channels": channels,
        "response_metadata": {"next_cursor": cursor},
    }


def _known(channel_id, **over):
    base = {
        "channel_id": channel_id, "mode": "active", "membership": "joined",
        "writer_owner": "archiver", "cutover_ts": "1700000000.000000",
        "operator_hold": False, "channel_name": "", "is_private": False,
    }
    return {**base, **over}


NOW = 1790000000.0


def _db(live: bool = True) -> FakeArchivingRepo:
    """DB 함수 규칙을 흉내 내는 저장소. 시계를 고정한다."""
    repo = FakeArchivingRepo()
    repo.clock = lambda: NOW
    repo.given_flag("archiver_writes_live", live)
    return repo


def _row(repo, channel_id="C1"):
    return repo.channel_rows[("tyit", channel_id)]


def _assert_single_writer(row):
    """두 writer 금지 — 주인은 하나이고, 모드와 주인이 어긋나지 않는다."""
    assert row["writer_owner"] in {"master", "archiver"}
    if row["mode"] == "active":
        assert row["writer_owner"] == "archiver" and row["cutover_ts"]
    if row["mode"] in {"off", "shadow"}:
        assert row["writer_owner"] == "master"


# --- 무엇을 가져오나 -----------------------------------------------------------

def test_only_joined_channels_are_fetched():
    """앱 설치만으로 전 채널을 수집하지 않는다. `users.conversations` 는 참여분만 준다."""
    slack = FakeSlack([_page([{"id": "C1", "name": "팀_자금"}])])

    membership.fetch_joined(slack)

    assert slack.calls[0]["types"] == "public_channel,private_channel"
    assert slack.calls[0]["exclude_archived"] is True


def test_dms_never_enter_even_if_slack_returns_them():
    """DM 은 개인 작업공간이다. 타입으로 막고 값으로 한 번 더 막는다."""
    slack = FakeSlack([_page([
        {"id": "C1", "name": "팀_자금"},
        {"id": "D9", "is_im": True},
        {"id": "G8", "is_mpim": True},
    ])])

    got = membership.fetch_joined(slack)

    assert [c.channel_id for c in got] == ["C1"]


def test_every_page_is_read():
    """한 페이지만 읽으면 채널이 늘어난 날부터 뒤쪽이 조용히 빠진다."""
    slack = FakeSlack([
        _page([{"id": "C1"}], cursor="next"),
        _page([{"id": "C2"}], cursor=""),
    ])

    got = membership.fetch_joined(slack)

    assert [c.channel_id for c in got] == ["C1", "C2"]
    assert slack.calls[1]["cursor"] == "next"


def test_private_channels_are_included():
    """비공개 채널도 초대됐으면 수집 대상이다(오너 지시 3)."""
    slack = FakeSlack([_page([{"id": "G1", "name": "비공개", "is_private": True}])])

    (got,) = membership.fetch_joined(slack)

    assert got.is_private is True


def test_a_channel_name_off_the_convention_is_still_collected():
    """이름 규칙은 사람이 찾기 위한 것이다. 규칙으로 거르면 초대가 무시된다."""
    slack = FakeSlack([_page([{"id": "C9", "name": "잡담방"}])])

    plan = membership.plan_sync(membership.fetch_joined(slack), [])

    assert [a.channel_id for a in plan.registered] == ["C9"]


# --- 초대·제거·재초대 ----------------------------------------------------------

def test_an_invited_channel_is_registered_for_live_collection():
    """초대는 곧 운영 수집이다(2026-11-18 오너 결정, shadow 폐기)."""
    plan = membership.plan_sync([Channel("C1", "팀_자금")], [])

    (action,) = plan.registered
    assert action.mode == "active"
    assert action.membership == "joined"


def test_no_sync_plan_ever_requests_shadow():
    """어떤 기존 상태에서 출발해도 계획에 shadow 가 나오지 않는다."""
    known = [
        _known("C-A"),
        _known("C-B", mode="paused", membership="left"),
        _known("C-C", mode="paused", writer_owner="master", cutover_ts=""),
        _known("C-D", mode="off", writer_owner="master", cutover_ts=""),
        _known("C-E", mode="shadow", writer_owner="master", cutover_ts=""),
        _known("C-F", mode="paused", operator_hold=True),
        _known("C-G", mode="paused", membership="left", writer_owner="archiver"),
    ]
    joined = [Channel(row["channel_id"]) for row in known] + [Channel("C-NEW")]

    plan = membership.plan_sync(joined, known)

    assert plan.actions
    assert all(action.mode != "shadow" for action in plan.actions)


def test_a_new_invite_is_stored_active_and_owned_by_the_archiver():
    """DB 는 주인·좌표를 한 번에 정한다. 좌표는 등록 시각 + 정착 시간이다."""
    repo = _db()
    plan = membership.plan_sync([Channel("C1", "팀_자금")], [])

    counts = membership.apply_plan(repo, "tyit", plan)

    row = _row(repo)
    assert counts["registered"] == 1
    assert (row["mode"], row["writer_owner"], row["membership"]) == (
        "active", "archiver", "joined",
    )
    assert float(row["cutover_ts"]) == NOW + SETTLE_SECONDS
    assert SETTLE_SECONDS == membership.SETTLE_SECONDS
    _assert_single_writer(row)
    assert ("tyit", "C1") in repo.locked


def test_a_new_invite_is_deferred_while_the_live_switch_is_off():
    """넘기면 Master 는 멈추고 Archiver 는 안 쓴다 — 행을 만들지 않아 Master 가 계속 쓴다."""
    repo = _db(live=False)

    counts = membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], []),
    )

    assert counts["deferred"] == 1 and counts["registered"] == 0
    assert repo.channel_rows == {}


def test_removing_the_bot_stops_collection():
    """봇이 없는 채널에서 수집을 이어 갈 방법은 없다. 목록에만 남으면 오류만 쌓인다."""
    plan = membership.plan_sync([], [_known("C1")])

    (action,) = plan.stopped
    assert action.mode == "paused"
    assert action.membership == "left"


def test_a_channel_already_marked_left_is_not_touched_again():
    """매번 다시 쓰면 `updated_by` 가 바뀌어 **언제 빠졌는지**가 흐려진다."""
    plan = membership.plan_sync([], [_known("C1", membership="left", mode="paused")])

    assert plan.actions == []


def test_re_inviting_resumes_live_collection():
    plan = membership.plan_sync(
        [Channel("C1")], [_known("C1", membership="left", mode="paused")],
    )

    (action,) = plan.resumed
    assert action.mode == "active"


@pytest.mark.parametrize("membership_value", ["left", "joined"])
def test_re_inviting_a_former_live_writer_keeps_its_cutover(membership_value):
    """이미 Archiver 몫이던 채널은 기존 좌표를 보존한다(새로 박으면 이력이 지워진다)."""
    repo = _db()
    repo.given_channel("tyit", "C1", "paused", owner="archiver",
                       cutover_ts="1700000000.000100", membership=membership_value)

    membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], repo.channels("tyit")),
    )

    row = _row(repo)
    assert (row["mode"], row["writer_owner"]) == ("active", "archiver")
    assert row["cutover_ts"] == "1700000000.000100"
    _assert_single_writer(row)
    assert not [a for a in repo.audit_rows if a["field"] == "writer_owner"]


@pytest.mark.parametrize("prior_mode", ["paused", "off", "shadow"])
def test_re_inviting_a_master_owned_channel_hands_it_over_with_a_new_cutover(prior_mode):
    repo = _db()
    repo.given_channel("tyit", "C1", prior_mode, owner="master",
                       membership="left" if prior_mode == "paused" else "joined")

    membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], repo.channels("tyit")),
    )

    row = _row(repo)
    assert (row["mode"], row["writer_owner"]) == ("active", "archiver")
    assert float(row["cutover_ts"]) == NOW + SETTLE_SECONDS
    _assert_single_writer(row)
    (owner_audit,) = [a for a in repo.audit_rows if a["field"] == "writer_owner"]
    assert owner_audit["old_value"].startswith("master@")
    assert owner_audit["new_value"].startswith("archiver@")


def test_a_master_owned_re_invite_stays_with_master_while_the_switch_is_off():
    repo = _db(live=False)
    repo.given_channel("tyit", "C1", "paused", owner="master", membership="left")

    membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], repo.channels("tyit")),
    )

    row = _row(repo)
    assert (row["mode"], row["writer_owner"]) == ("off", "master")
    _assert_single_writer(row)


def test_a_cutover_never_moves_backwards_on_hand_over():
    """역인수 좌표가 미래에 있으면 그 좌표를 쓴다(좌표는 앞으로만 간다)."""
    repo = _db()
    future = f"{NOW + 3600:.6f}"
    repo.given_channel("tyit", "C1", "paused", owner="master", cutover_ts=future,
                       membership="left")

    membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], repo.channels("tyit")),
    )

    assert _row(repo)["cutover_ts"] == future


def test_re_inviting_does_not_revive_a_channel_someone_turned_off():
    """**이 시험이 `operator_hold` 의 이유다.**

    멤버십 상실과 사람이 끈 것을 한 칸에 담으면, 재초대 한 번에 끈 채널까지
    되살아난다. 끈 사람은 그 사실을 모른다.
    """
    plan = membership.plan_sync(
        [Channel("C1")],
        [_known("C1", membership="left", mode="paused", operator_hold=True)],
    )

    assert plan.resumed == []
    assert [a.mode for a in plan.actions] == ["paused"]


@pytest.mark.parametrize("owner", ["master", "archiver"])
def test_an_operator_hold_survives_re_invite_in_the_database(owner):
    repo = _db()
    repo.given_channel("tyit", "C1", "paused", owner=owner,
                       cutover_ts="1700000000.000000" if owner == "archiver" else "",
                       membership="left", operator_hold=True)

    membership.apply_plan(
        repo, "tyit", membership.plan_sync([Channel("C1")], repo.channels("tyit")),
    )

    row = _row(repo)
    assert (row["mode"], row["membership"], row["writer_owner"]) == (
        "paused", "joined", owner,
    )


def test_an_active_channel_that_loses_the_bot_is_paused_too():
    """운영 수집 중이어도 봇이 없으면 멈춘다."""
    plan = membership.plan_sync([], [_known("C1", mode="active")])

    assert [a.mode for a in plan.stopped] == ["paused"]


# --- 자동 전환 금지 -------------------------------------------------------------

def test_sync_refuses_a_plan_that_asks_for_shadow():
    """shadow 는 폐기됐다. 그 값을 요청하는 계획은 조용히 통과시키지 않는다."""
    repo = FakeRepo()
    plan = membership.SyncPlan([membership.Action(
        kind="register", channel_id="C1", mode="shadow", membership="joined",
        reason="옛 계획",
    )])

    with pytest.raises(ValueError, match="shadow"):
        membership.apply_plan(repo, "tyit", plan)
    assert repo.rows == {}


def test_applying_writes_membership_and_audits_changes():
    repo = FakeRepo()
    plan = membership.plan_sync([Channel("C1", "팀_자금", True)], [])

    counts = membership.apply_plan(repo, "tyit", plan)

    assert counts["registered"] == 1
    row = repo.rows["C1"]
    assert row["membership"] == "joined" and row["mode"] == "active"
    assert row["is_private"] is True
    assert repo.audit[0]["reason"].startswith("봇이 채널에 초대돼")


def test_merely_confirming_membership_is_not_audited():
    """확인마다 기록을 남기면 **진짜 변경이 그 안에 묻힌다.**"""
    repo = FakeRepo([_known("C1")])
    plan = membership.plan_sync([Channel("C1")], repo.channels("tyit"))

    membership.apply_plan(repo, "tyit", plan)

    assert repo.audit == []


# --- 이벤트가 먼저 온 채널 ------------------------------------------------------

def test_an_event_from_a_new_channel_registers_after_checking_membership():
    """초대 직후 첫 메시지가 동기화보다 먼저 올 수 있다. 버리면 그 대화는 영영 없다."""
    repo = FakeRepo()
    slack = FakeSlack([_page([{"id": "C1", "name": "새방"}])])

    assert membership.ensure_registered(slack, repo, "tyit", "C1") is True
    assert repo.rows["C1"]["mode"] == "active"


def test_event_discovery_and_periodic_sync_store_the_same_state():
    """두 입구(이벤트 발견·주기 동기화)가 같은 채널에 다른 상태를 만들지 않는다."""
    by_event, by_sync = _db(), _db()
    slack = FakeSlack([_page([{"id": "C1", "name": "새방"}])])

    membership.ensure_registered(slack, by_event, "tyit", "C1")
    membership.sync(FakeSlack([_page([{"id": "C1", "name": "새방"}])]), by_sync, "tyit")
    membership.sync(FakeSlack([_page([{"id": "C1", "name": "새방"}])]), by_event, "tyit")

    keys = ("mode", "writer_owner", "cutover_ts", "membership")
    assert {k: _row(by_event)[k] for k in keys} == {k: _row(by_sync)[k] for k in keys}
    # 두 번째 확인은 좌표를 다시 박지 않는다
    assert len([a for a in by_event.audit_rows if a["field"] == "writer_owner"]) == 1


def test_an_event_from_a_channel_we_are_not_in_is_ignored():
    """확인 없이 등록하면 **초대 없는 채널이 이벤트 하나로** 수집 대상이 된다."""
    repo = FakeRepo()
    slack = FakeSlack([_page([{"id": "C1"}])])

    assert membership.ensure_registered(slack, repo, "tyit", "C-OTHER") is False
    assert repo.rows == {}


def test_a_known_channel_does_not_call_slack_again():
    """이벤트마다 Slack 을 부르면 rate limit 에 걸려 수집이 멈춘다."""
    repo = FakeRepo([_known("C1")])
    slack = FakeSlack([])

    assert membership.ensure_registered(slack, repo, "tyit", "C1") is False
    assert slack.calls == []


def test_event_discovery_does_not_mark_other_channels_as_left():
    """한 채널의 첫 이벤트가 기존 다른 채널의 멤버십을 바꾸면 안 된다."""
    repo = FakeRepo([_known("C-EXISTING")])
    slack = FakeSlack([_page([{"id": "C-NEW", "name": "새방"}])])

    assert membership.ensure_registered(slack, repo, "tyit", "C-NEW") is True

    assert repo.rows["C-EXISTING"]["membership"] == "joined"
    assert repo.rows["C-NEW"]["membership"] == "joined"


def test_event_after_rejoin_refreshes_a_left_channel():
    repo = FakeRepo([_known("C1", membership="left", mode="paused")])
    slack = FakeSlack([_page([{"id": "C1", "name": "다시초대"}])])

    assert membership.ensure_registered(slack, repo, "tyit", "C1") is True

    assert repo.rows["C1"]["membership"] == "joined"
    assert repo.rows["C1"]["mode"] == "active"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_known("C1"), True),
        (_known("C1", mode="active"), True),
        (_known("C1", mode="paused"), False),
        (_known("C1", membership="left"), False),
        (_known("C1", operator_hold=True), False),
        (None, False),
    ],
)
def test_collectible_requires_joined_unheld_collection_mode(row, expected):
    assert membership.is_collectible(row) is expected


# --- 전체 동기화 ----------------------------------------------------------------

def test_sync_reports_what_changed():
    repo = FakeRepo([_known("C-GONE"), _known("C-KEEP")])
    slack = FakeSlack([_page([{"id": "C-KEEP"}, {"id": "C-NEW", "name": "새방"}])])

    result = membership.sync(slack, repo, "tyit")

    assert result == {
        "registered": 1, "resumed": 0, "stopped": 1, "checked": 1, "deferred": 0,
        "joined": 2,
    }
    assert repo.rows["C-GONE"]["membership"] == "left"
    assert repo.rows["C-NEW"]["mode"] == "active"


def test_a_failed_slack_invite_registers_nothing():
    """초대가 실패하면 Archiver 는 그 채널 멤버가 아니다 — 동기화가 행을 만들지 않는다.

    Master 는 행이 없는 채널을 계속 쓴다(write_owner). 실패가 「아무도 안 쓰는
    채널」 이 되지 않는다.
    """
    repo = _db()
    slack = FakeSlack([_page([])])  # 초대 실패: users.conversations 에 없다

    result = membership.sync(slack, repo, "tyit")

    assert result["registered"] == 0
    assert repo.channel_rows == {}
