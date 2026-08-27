"""자동 참여 — 규칙에 맞는 공개 채널만, 비공개는 초대 필요 목록으로."""
from __future__ import annotations

from tybot.autojoin import on_channel_event, sweep


class FakeClient:
    def __init__(self, channels, fail=()):
        self._channels = channels
        self._fail = set(fail)
        self.joined: list[str] = []

    def conversations_list(self, **kw):
        return {"channels": self._channels, "response_metadata": {}}

    def conversations_join(self, channel):
        if channel in self._fail:
            raise RuntimeError("not_allowed")
        self.joined.append(channel)
        return {"ok": True}


def _ch(cid, name, *, member=False, private=False):
    return {"id": cid, "name": name, "is_member": member, "is_private": private}


def test_joins_rule_matching_public_channels():
    c = FakeClient([
        _ch("C1", "팀-전산_ABB110-주간회의"),
        _ch("C2", "현장-김해외동_180182-채팅방"),
    ])
    r = sweep(c)
    assert set(c.joined) == {"C1", "C2"}
    assert len(r.joined) == 2 and not r.failed


def test_skips_channels_that_break_the_rule():
    """잡담·개인 채널은 이름만으로 걸러진다 - 아카이브에 들어가면 안 된다."""
    c = FakeClient([_ch("C1", "점심메뉴"), _ch("C2", "random"), _ch("C3", "전사_공지")])
    r = sweep(c)
    assert c.joined == []
    assert len(r.skipped_rule) == 3


def test_private_channels_go_to_need_invite():
    """Slack 설계상 봇은 비공개 채널에 스스로 못 들어간다."""
    c = FakeClient([_ch("C1", "팀-인사_HR100-비밀", private=True)])
    r = sweep(c)
    assert c.joined == []
    assert r.need_invite == ["#팀-인사_HR100-비밀"]


def test_already_member_is_not_rejoined():
    c = FakeClient([_ch("C1", "팀-전산_ABB110-주간회의", member=True)])
    r = sweep(c)
    assert c.joined == [] and r.already == ["#팀-전산_ABB110-주간회의"]


def test_one_failure_does_not_stop_the_sweep():
    c = FakeClient(
        [_ch("C1", "팀-전산_ABB110-회의"), _ch("C2", "팀-자금_ABB540-보고")], fail=("C1",)
    )
    r = sweep(c)
    assert c.joined == ["C2"]
    assert len(r.failed) == 1 and len(r.joined) == 1


def test_dry_run_joins_nothing():
    c = FakeClient([_ch("C1", "팀-전산_ABB110-회의")])
    r = sweep(c, dry_run=True)
    assert c.joined == [] and r.joined == ["#팀-전산_ABB110-회의"]


def test_new_channel_event_joins_immediately():
    """새 채널이 규칙에 맞으면 다음 스윕까지 기다리지 않는다."""
    c = FakeClient([])
    assert on_channel_event(c, _ch("C9", "팀-신규_NEW1-킥오프")) == "#팀-신규_NEW1-킥오프"
    assert c.joined == ["C9"]


def test_new_channel_event_ignores_non_matching():
    c = FakeClient([])
    assert on_channel_event(c, _ch("C9", "잡담방")) is None
    assert c.joined == []


def test_rename_into_the_rule_starts_collection():
    """이름을 규칙에 맞게 고치면 그 순간부터 수집 대상이 된다."""
    c = FakeClient([])
    assert on_channel_event(c, _ch("C9", "팀-전산_ABB110-주간회의")) is not None


def test_summary_line():
    c = FakeClient([_ch("C1", "팀-전산_ABB110-회의"), _ch("C2", "점심")])
    assert "autojoin joined=1" in sweep(c).summary()
