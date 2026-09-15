"""콘솔 채널 관리 — 담당자 없는 채널을 찾아 한 번에 정한다.

설계: `docs/design/console-channel-admin.md`

## 재현하는 사례
옛날에 만든 채널이 `/채널 수정` 이 안 된다. 담당자가 없기 때문이다. 담당자가 없으면
검토자도 못 정하고, 그러면 첨부 검수 DM 도 안 간다 — 한 칸이 비어서 그 뒤 기능이
줄줄이 멈춘다.
"""
from __future__ import annotations

import pytest

from tybot.console import channel_admin as ca

WS = "tyit"
LABEL = "전산팀"


def _doc(channel="#팀-전산_ABB155-주간보고", channel_id="C1", **over):
    row = {
        "workspace": WS,
        "workspaceLabel": LABEL,
        "channel": channel,
        "channelId": channel_id,
        "lines": 40,
        "attachmentLines": 5,
        "lastIngestedAt": "2026-09-14T17:00+09:00",
    }
    row.update(over)
    return row


def _rows(docs=None, *, owners=None, reviewers=None, answers=None):
    return ca.build_rows(
        docs if docs is not None else [_doc()],
        owners=owners or {},
        reviewers=reviewers or {},
        answers=answers or {},
    )


# --- 표 만들기 ----------------------------------------------------------------
def test_one_row_per_channel_not_per_document():
    """한 채널에 날짜별 문서가 여럿이다. 줄이 날짜마다 생기면 표가 못 쓰게 된다."""
    docs = [_doc(lines=10), _doc(lines=30), _doc(lines=5)]
    (row,) = _rows(docs)
    assert row.documents == 3
    assert row.lines == 45


def test_a_heartbeat_only_channel_is_visible_without_becoming_a_document():
    (row,) = _rows([
        _doc(documents=0, lines=0, attachmentLines=0, lastIngestedAt="")
    ])

    assert row.channel_id == "C1"
    assert row.documents == 0
    assert row.lines == 0


def test_heartbeat_channels_are_available_before_the_first_archive_document(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    status = tmp_path / "status" / "tyit.json"
    status.parent.mkdir(parents=True)
    status.write_text(
        '{"channel_rows":[{"id":"C_NEW","name":"#팀-전산_abb155-신규"}]}',
        encoding="utf-8",
    )

    assert ca.heartbeat_channel_rows() == [
        {
            "workspace": "tyit",
            "workspaceLabel": "tyit",
            "channelId": "C_NEW",
            "channel": "#팀-전산_abb155-신규",
            "documents": 0,
            "lines": 0,
            "attachmentLines": 0,
        }
    ]


def test_channels_are_joined_by_id_not_name():
    """이름은 바뀐다. 이름으로 이으면 담당자가 조용히 다른 채널에 붙는다."""
    docs = [_doc(channel="#옛이름", channel_id="C1"), _doc(channel="#새이름", channel_id="C1")]
    rows = _rows(docs)
    assert len(rows) == 1


def test_different_channels_stay_separate():
    rows = _rows([_doc(channel_id="C1"), _doc(channel="#다른", channel_id="C2")])
    assert len(rows) == 2


def test_last_ingested_is_the_newest():
    docs = [
        _doc(lastIngestedAt="2026-09-01T09:00+09:00"),
        _doc(lastIngestedAt="2026-09-14T17:00+09:00"),
    ]
    (row,) = _rows(docs)
    assert row.last_ingested.startswith("2026-09-14")


def test_owner_and_reviewer_are_attached():
    key = (WS, "C1")
    (row,) = _rows(
        owners={key: {"owner_user_id": "U1", "manager_user_ids": ["U9"],
                      "owner_source": "console"}},
        reviewers={key: [{"reviewer_user": "U2", "send_at": "09:00"}]},
        answers={key: (12, "2026-09-14T10:00+09:00")},
    )
    assert row.owner == "U1"
    assert row.managers == ["U9"]
    assert row.reviewers == ["U2"]
    assert row.send_at == "09:00"
    assert row.answers == 12
    assert not row.needs_owner
    assert not row.needs_reviewer


def test_missing_owner_is_flagged():
    (row,) = _rows()
    assert row.needs_owner
    assert row.needs_reviewer
    assert row.owner == ""


def test_documents_without_a_channel_id_still_appear():
    """v1 아카이브 문서다. 안 보이면 「채널이 없다」 로 읽힌다."""
    (row,) = _rows([_doc(channelId="")])
    assert row.channel_id == ""
    assert row.documents == 1


def test_rows_without_workspace_or_channel_are_dropped():
    assert _rows([_doc(workspace=""), _doc(channel="")]) == []


# --- 정렬: 급한 것이 위로 -----------------------------------------------------
def test_channels_people_actually_use_come_first():
    """답변이 많은데 담당자가 없는 곳이 가장 급하다."""
    docs = [
        _doc(channel="#조용한", channel_id="C1"),
        _doc(channel="#바쁜", channel_id="C2"),
    ]
    rows = _rows(docs, answers={(WS, "C2"): (50, "2026-09-14T10:00+09:00")})
    assert rows[0].channel == "#바쁜"


def test_channels_with_an_owner_sink_below():
    docs = [_doc(channel="#있음", channel_id="C1"), _doc(channel="#없음", channel_id="C2")]
    rows = _rows(docs, owners={(WS, "C1"): {"owner_user_id": "U1"}})
    assert rows[0].channel == "#없음"


def test_owner_missing_outranks_reviewer_missing():
    """담당자가 없으면 검토자를 정할 수도 없다. 순서가 있다."""
    docs = [_doc(channel="#검토자만없음", channel_id="C1"),
            _doc(channel="#담당자없음", channel_id="C2")]
    rows = _rows(
        docs,
        owners={(WS, "C1"): {"owner_user_id": "U1"}},
        reviewers={(WS, "C2"): [{"reviewer_user": "U2", "send_at": "09:00"}]},
    )
    assert rows[0].channel == "#담당자없음"


# --- 머리글 숫자 --------------------------------------------------------------
def test_summary_says_what_to_do_first():
    docs = [_doc(channel=f"#c{i}", channel_id=f"C{i}") for i in range(4)]
    rows = _rows(
        docs,
        owners={(WS, "C0"): {"owner_user_id": "U1"}},
        answers={(WS, "C1"): (7, "2026-09-14T10:00+09:00")},
    )
    got = ca.summary(rows)
    assert got["channels"] == 4
    assert got["missingOwner"] == 3
    assert got["missingReviewer"] == 4
    # 담당자가 없는데 사람이 쓰는 채널. 이게 급한 것이다.
    assert got["activeMissingOwner"] == 1


def test_summary_of_nothing():
    assert ca.summary([])["channels"] == 0


# --- 일괄 지정 ----------------------------------------------------------------
class FakeStore:
    def __init__(self, existing=None, fail=()):
        self.owners = dict(existing or {})
        self.fail = set(fail)
        self.calls: list[dict] = []

    def set_owner(self, workspace, channel_id, owner, *, set_by, name="", overwrite=False):
        self.calls.append({
            "workspace": workspace, "channel_id": channel_id, "owner": owner,
            "set_by": set_by, "name": name, "overwrite": overwrite,
        })
        if channel_id in self.fail:
            raise RuntimeError("잠김")
        current = self.owners.get(channel_id)
        if current and not overwrite:
            return False
        if current == owner:
            return False
        self.owners[channel_id] = owner
        return True


def test_bulk_assignment_changes_every_empty_channel():
    store = FakeStore()
    got = ca.assign_owner(
        store,
        [(WS, "C1", "#a"), (WS, "C2", "#b")],
        "U1", actor="dan@taeyoung.com",
    )
    assert got.changed == ["#a", "#b"]
    assert got.ok


def test_existing_owner_is_not_overwritten_by_default():
    """기존 담당자는 실제 요청자일 수 있다. 덮으면 그 사람이 권한을 잃는다."""
    store = FakeStore({"C1": "U9"})
    got = ca.assign_owner(store, [(WS, "C1", "#a")], "U1", actor="dan@t.com")
    assert got.changed == []
    assert got.skipped == [("#a", "이미 담당자가 있습니다")]
    assert store.owners["C1"] == "U9"


def test_overwrite_is_possible_when_asked():
    store = FakeStore({"C1": "U9"})
    got = ca.assign_owner(
        store, [(WS, "C1", "#a")], "U1", actor="dan@t.com", overwrite=True
    )
    assert got.changed == ["#a"]
    assert store.owners["C1"] == "U1"


def test_partial_failure_is_reported_as_partial():
    """전부 성공으로 보이면 사람은 확인하지 않는다."""
    store = FakeStore(fail={"C2"})
    got = ca.assign_owner(
        store, [(WS, "C1", "#a"), (WS, "C2", "#b"), (WS, "C3", "#c")],
        "U1", actor="dan@t.com",
    )
    assert got.changed == ["#a", "#c"]
    assert [c for c, _ in got.skipped] == ["#b"]
    assert not got.ok
    assert "1건 변경 없음" in got.message()


def test_one_failure_does_not_stop_the_rest():
    """첫 실패에서 멈추면 사람이 다시 눌러야 하고 무엇이 됐는지도 모른다."""
    store = FakeStore(fail={"C1"})
    ca.assign_owner(
        store, [(WS, "C1", "#a"), (WS, "C2", "#b")], "U1", actor="dan@t.com"
    )
    assert store.owners.get("C2") == "U1"


def test_channels_without_an_id_are_refused():
    """이름으로 쓰면 다른 채널에 붙을 수 있다."""
    store = FakeStore()
    got = ca.assign_owner(
        store, [(WS, "name:#옛날", "#옛날")], "U1", actor="dan@t.com"
    )
    assert got.changed == []
    assert "채널 ID" in got.skipped[0][1]
    assert store.calls == []


def test_empty_owner_is_refused():
    with pytest.raises(ValueError, match="담당자"):
        ca.assign_owner(FakeStore(), [(WS, "C1", "#a")], "  ", actor="dan@t.com")


def test_actor_is_recorded():
    """나중에 「이 사람이 왜 담당자인가」 를 답할 수 있어야 한다."""
    store = FakeStore()
    ca.assign_owner(store, [(WS, "C1", "#a")], "U1", actor="dan@taeyoung.com")
    assert store.calls[0]["set_by"] == "dan@taeyoung.com"


def test_result_is_json_ready():
    store = FakeStore({"C2": "U9"})
    got = ca.assign_owner(
        store, [(WS, "C1", "#a"), (WS, "C2", "#b")], "U1", actor="dan@t.com"
    )
    payload = got.to_json()
    assert payload["changed"] == ["#a"]
    assert payload["skipped"][0]["channel"] == "#b"
    assert payload["ok"] is False
