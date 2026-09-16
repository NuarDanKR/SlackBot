"""보관·삭제된 채널을 근거에서 뺀다(B-51).

원문은 지우지 않는다(원칙 1). 빼는 것은 **근거로 쓰는 것**뿐이다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import channel_lifecycle as cl


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("INCLUDE_RETIRED_CHANNELS", raising=False)
    cl._cache.clear()
    return tmp_path


def _doc(**kw):
    base = {"workspace": "tyit", "channel": "#팀-전산_ABB155-공지", "channel_id": "C155"}
    base.update(kw)
    return SimpleNamespace(**base)


# --- 기본값 --------------------------------------------------------------------
def test_retired_channels_are_excluded_by_default():
    """운영이 켜기 전에는 **근거에 들어가지 않는다**(원칙 3)."""
    assert cl.include_retired() is False
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    assert cl.keep(_doc()) is False


def test_a_live_channel_is_never_touched():
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    assert cl.keep(_doc(channel_id="C999", channel="#팀-자금_ABB540-주간")) is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_setting_turns_inclusion_back_on(monkeypatch, value):
    monkeypatch.setenv("INCLUDE_RETIRED_CHANNELS", value)
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    assert cl.include_retired() is True
    assert cl.keep(_doc()) is True


def test_an_unreadable_registry_excludes_nothing(_state):
    """기록을 못 읽으면 **아무것도 빼지 않는다.**

    막는 쪽으로 기울면 디스크 오류 하나로 멀쩡한 자료가 통째로 사라지고, 봇은
    "자료가 없다" 고 답한다 — 사람은 그것을 수집 실패로 읽는다.
    """
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    cl.state_path().write_text("{ 깨진 json", encoding="utf-8")
    cl._cache.clear()
    assert cl.keep(_doc()) is True


# --- 신원 판정 -----------------------------------------------------------------
def test_identity_comes_from_the_id_not_the_name():
    """이름은 바뀌고 재사용된다. **같은 이름의 새 채널까지 빼면 안 된다.**"""
    cl.mark("tyit", "C-old", channel="#팀-전산_ABB155-공지")
    # 같은 이름으로 새로 만든 채널. ID 가 다르므로 그대로 근거가 된다.
    assert cl.keep(_doc(channel_id="C-new")) is True
    assert cl.keep(_doc(channel_id="C-old")) is False


def test_legacy_documents_without_a_real_id_fall_back_to_the_name():
    """만들어 낸 ID 는 신원이 아니다 — 그때만 이름으로 찾는다."""
    cl.mark("tyit", "", channel="#팀-전산_ABB155-공지")
    assert cl.keep(_doc(channel_id="legacy-abc")) is False
    assert cl.keep(_doc(channel_id="legacy-abc", channel="#다른채널")) is True


def test_the_registry_is_scoped_to_one_workspace():
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    assert cl.keep(_doc(workspace="mgmt")) is True


# --- 되돌리기 -----------------------------------------------------------------
def test_unarchiving_brings_the_channel_back_as_evidence():
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    assert cl.restore("tyit", "C155") is True
    assert cl.keep(_doc()) is True
    # 없는 것을 지우면 아무 일도 없다.
    assert cl.restore("tyit", "C155") is False


def test_the_record_never_carries_message_text(_state):
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지", reason=cl.DELETED)
    raw = cl.state_path().read_text(encoding="utf-8")
    data = json.loads(raw)
    row = data["channels"][0]
    assert set(row) == {"workspace", "channel_id", "channel", "reason", "at"}
    assert row["reason"] == cl.DELETED


def test_an_unknown_reason_is_refused():
    with pytest.raises(ValueError):
        cl.mark("tyit", "C155", reason="made-up")


# --- 출처 표시 -----------------------------------------------------------------
def test_included_sources_say_so(monkeypatch):
    """포함으로 켜도 **조용히 섞지 않는다.**"""
    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지", reason=cl.ARCHIVED)
    assert cl.mark_for("tyit", "C155", "#팀-전산_ABB155-공지") == " (보관 채널)"
    cl.mark("tyit", "C900", channel="#팀-자금_ABB540-주간", reason=cl.DELETED)
    assert cl.mark_for("tyit", "C900", "#팀-자금_ABB540-주간") == " (삭제 채널)"
    assert cl.mark_for("tyit", "C-live", "#살아있는채널") == ""


def test_the_citation_carries_the_mark(monkeypatch):
    from tybot.archive.store import SearchHit

    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    doc = SimpleNamespace(
        workspace="tyit", channel="#팀-전산_ABB155-공지", channel_id="C155",
        path=Path("2026-09-01.md"),
    )
    line = SimpleNamespace(ts="2026-09-01 10:00", source_path=None, lineno=1,
                           speaker="사람", text="공지")

    assert SearchHit(doc=doc, line=line, score=1).citation() == (
        "[전산팀]공지 (보관 채널), 📄2026-09-01.md(2026-09-01)"
    )


# --- 대조 ---------------------------------------------------------------------
class FakeClient:
    def __init__(self, channels: list[dict]):
        self.channels = channels

    def conversations_list(self, **kw):
        # 보관된 것까지 봐야 보관과 삭제를 구별할 수 있다.
        assert kw.get("exclude_archived") is False
        return {"channels": self.channels, "response_metadata": {}}


def test_reconcile_marks_archived_and_deleted_public_channels():
    client = FakeClient([
        {"id": "C1", "name": "a", "is_archived": True},
        {"id": "C2", "name": "b", "is_archived": False},
    ])

    got = cl.reconcile(client, "tyit", known=[("C1", "#a"), ("C2", "#b"), ("C3", "#c")])

    assert (got["archived"], got["deleted"]) == (1, 1)
    assert cl.keep(_doc(channel_id="C1", channel="#a")) is False
    assert cl.keep(_doc(channel_id="C3", channel="#c")) is False
    assert cl.keep(_doc(channel_id="C2", channel="#b")) is True


def test_reconcile_does_not_call_a_vanished_private_channel_deleted():
    """비공개가 목록에서 빠지는 이유는 둘이다 — 지워졌거나 **봇이 나갔거나.**

    구별할 수 없다. 나간 것을 삭제로 적으면 다시 초대해도 자료가 빠진 채로 남는다.
    """
    got = cl.reconcile(FakeClient([]), "tyit", known=[("G1", "#비공개")])

    assert got["deleted"] == 0
    assert cl.keep(_doc(channel_id="G1", channel="#비공개")) is True


def test_reconcile_restores_a_channel_that_came_back():
    cl.mark("tyit", "C1", channel="#a")
    got = cl.reconcile(
        FakeClient([{"id": "C1", "name": "a", "is_archived": False}]),
        "tyit", known=[("C1", "#a")],
    )
    assert got["restored"] == 1
    assert cl.keep(_doc(channel_id="C1", channel="#a")) is True


def test_a_failed_lookup_does_not_erase_the_record():
    """조회 실패로 기록을 지우면, 그 순간 없앤 채널이 전부 답에 돌아온다."""
    cl.mark("tyit", "C1", channel="#a")

    class Broken:
        def conversations_list(self, **kw):
            raise RuntimeError("rate limited")

    got = cl.reconcile(Broken(), "tyit", known=[("C1", "#a")])

    assert got["failed"] == 1
    assert cl.keep(_doc(channel_id="C1", channel="#a")) is False


# --- 콘솔 설정 -----------------------------------------------------------------
def test_the_console_setting_round_trips():
    from tybot.console import env_settings

    assert "INCLUDE_RETIRED_CHANNELS" in env_settings.MANAGED_STATIC_KEYS
    values = env_settings._validate({"includeRetiredChannels": True})
    assert values["INCLUDE_RETIRED_CHANNELS"] == "1"
    assert env_settings._validate({})["INCLUDE_RETIRED_CHANNELS"] == "0"


def test_the_console_snapshot_shows_what_would_change():
    """설정만 보여 주면 「켜면 뭐가 달라지는지」 를 알 수 없다."""
    from tybot.console import env_settings

    cl.mark("tyit", "C155", channel="#팀-전산_ABB155-공지")
    snap = env_settings.snapshot()

    assert snap["includeRetiredChannels"] is False
    assert snap["retiredChannels"]["count"] == 1
    item = snap["retiredChannels"]["items"][0]
    assert item["channel"] == "#팀-전산_ABB155-공지"
    assert item["reason"] == cl.ARCHIVED


# --- 실제 두 경로에서 빠지는가 -------------------------------------------------
#
# 사용자가 본 증상은 둘이었다 — **출처로 나오고**, **요약에 들어간다.**
# 모듈 단위 판정만 테스트하면 그 둘 중 하나만 고쳐도 통과한다.
def _archive(tmp_path) -> Path:
    from datetime import UTC, datetime

    from tybot.archive import writer

    for channel, channel_id, text in (
        ("#팀-전산_ABB155-공지", "C155", "살아있는 채널 기성금 3억"),
        ("#팀-자금_ABB540-주간", "C540", "없앤 채널 기성금 9억"),
    ):
        writer.ingest(
            tmp_path,
            workspace="tyit",
            channel=channel,
            channel_id=channel_id,
            messages=[writer.IncomingMessage(
                ts=datetime(2026, 9, 15, 9, 0, tzinfo=UTC), speaker="홍길동", text=text
            )],
            acl=[channel],
        )
    return tmp_path


def test_a_retired_channel_stops_being_a_source(tmp_path, monkeypatch):
    from tybot.access import RequestContext
    from tybot.archive.store import ArchiveStore

    root = _archive(tmp_path / "archive")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    cl._cache.clear()
    store = ArchiveStore(root)
    ctx = RequestContext(
        workspace="tyit",
        channels=frozenset({"#팀-전산_ABB155-공지", "#팀-자금_ABB540-주간"}),
    )

    assert len(store.search("기성금", ctx)) == 2

    cl.mark("tyit", "C540", channel="#팀-자금_ABB540-주간")
    hits = store.search("기성금", ctx)

    assert len(hits) == 1
    assert "9억" not in hits[0].line.text
    # 원문은 그대로 있다. 뺀 것은 **근거로 쓰는 것**뿐이다(원칙 1).
    assert any(d.channel == "#팀-자금_ABB540-주간" for d in store.docs())


def test_a_retired_channel_stops_feeding_the_summary(tmp_path, monkeypatch):
    from tybot import summary_review as sr
    from tybot.archive.store import ArchiveStore

    root = _archive(tmp_path / "archive")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    cl._cache.clear()
    store = ArchiveStore(root)

    assert sr.channel_source(store, "tyit", "C540", "")

    cl.mark("tyit", "C540", channel="#팀-자금_ABB540-주간")
    assert sr.channel_source(store, "tyit", "C540", "") == []

    # 켜면 다시 들어온다 — 운영이 정하는 자리다.
    monkeypatch.setenv("INCLUDE_RETIRED_CHANNELS", "1")
    assert sr.channel_source(store, "tyit", "C540", "")
