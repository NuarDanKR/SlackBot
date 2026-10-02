"""스위치가 아니라 **불변식**인 것들.

결정: 2026-10-02 오너 지시 — 수정·삭제 보존과 revision reader 의 fail-closed
동작은 끌 수 있는 기능이 아니다. 전에는 `preserve_edit_delete` ·
`revision_reader_ready` 라는 이름으로 운영 차단 조건에 올라 있었는데, 읽는 코드가
없어서 **아무것도 보증하지 않았다**(`test_production_flag_gates.py`).

필수 목록에서 뺀 것만으로는 부족하다. 목록에서 사라지면 다음 사람은 그것이
선택 사항이 됐다고 읽는다. 그래서 **동작 자체를** 여기서 지킨다.

## 왜 이 둘인가

둘 다 끄는 쪽에 안전한 상태가 없다.

- 수정·삭제를 안 쌓으면 사람이 고친 문장을 잃는다. 고친 문장도 원문이고,
  `message_changed` 는 지난 이벤트를 다시 주지 않으므로 **복구가 안 된다**
- revision reader 를 안 돌리면 지워진 문장이 근거로 나간다. 사람은 이미 그
  내용을 봤고, 그건 되돌릴 수 없다

그러니 「준비되면 켠다」 가 성립하지 않는다. 처음부터 참이어야 하는 것들이다.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tybot import archiving_bot
from tybot.archive import revision_reader as reader
from tybot.archive import store as archive_store
from tybot.archive.archiving_state import REQUIRED_PRODUCTION_FLAGS, UNENFORCED_FLAGS
from tybot.archive.store import RawLine

WS, CH = "tyit", "C12345678"
TS = "1790070000.000001"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_SOURCE": "env",
        "ARCHIVER_WORKSPACES": "tyit",
        "ARCHIVER_BOT_TOKEN_TYIT": "archiver-bot",
        "ARCHIVER_APP_TOKEN_TYIT": "archiver-app",
        "ARCHIVER_TEAM_ID_TYIT": "T12345678",
        "SLACK_BOT_TOKEN_TYIT": "master-bot",
        "SLACK_APP_TOKEN_TYIT": "master-app",
        "ARCHIVER_MASTER_BOT_USER_TYIT": "U_MASTER",
        "ARCHIVER_CHANNEL_IDS_TYIT": CH,
        "ARCHIVE_DIR": str(tmp_path / "live"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "shadow" / "archive"),
    }


class _Client:
    def users_info(self, user):
        return {"user": {"name": user}}

    def conversations_info(self, channel):
        return {"channel": {"id": channel, "name": "팀_전산(ABB155)_공지",
                            "is_private": False, "is_member": True}}

    def conversations_members(self, channel, limit=200, cursor=None):
        return {"members": [], "response_metadata": {}}


def _collector(tmp_path: Path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    collector._record_revision = lambda **_values: True
    return collector


def _changed_event() -> dict:
    return {
        "channel_type": "channel",
        "channel": CH,
        "subtype": "message_changed",
        "event_ts": "1790070100.000001",
        "message": {"user": "U12345678", "ts": TS, "text": "회의는 11시입니다.",
                    "edited": {"ts": "1790070100.000001"}},
        "previous_message": {"user": "U12345678", "ts": TS, "text": "회의는 10시입니다."},
    }


def _deleted_event() -> dict:
    return {
        "channel_type": "channel",
        "channel": CH,
        "subtype": "message_deleted",
        "event_ts": "1790070100.000001",
        "deleted_ts": TS,
        "previous_message": {"user": "U12345678", "ts": TS, "text": "지울 문장"},
    }


def _raw_text(tmp_path: Path) -> str:
    return next(
        (tmp_path / "shadow").glob("workspaces/*/channels/*/raw/*.md")
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 불변식 1 — 수정·삭제는 **항상** 쌓인다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (_changed_event(), ("[수정 전] 회의는 10시입니다.", "[수정 후] 회의는 11시입니다.")),
        (_deleted_event(), ("[삭제 전] 지울 문장", "[삭제됨]")),
    ],
    ids=["changed", "deleted"],
)
def test_edit_and_delete_are_always_archived(tmp_path, monkeypatch, event, expected):
    """DB 가 없어도, 어떤 스위치가 꺼져 있어도 **쌓는다.**

    DB 를 통째로 끊어 둔다. 스위치를 읽으려면 DB 를 봐야 하므로, 이 상태에서
    동작이 유지된다는 것은 **어떤 스위치도 이 경로를 막지 못한다**는 뜻이다.
    나중에 누가 게이트를 넣으면(= 꺼진 값으로 읽혀 버려지면) 여기서 걸린다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    collector = _collector(tmp_path)

    assert collector.ingest_event(_Client(), event) == "revision-written"

    text = _raw_text(tmp_path)
    for fragment in expected:
        assert fragment in text


def test_no_feature_switch_guards_the_edit_and_delete_path(tmp_path, monkeypatch):
    """수정·삭제를 **버리는** 분기가 생기지 않았는지 본다.

    위 시험은 「지금 쌓인다」 를 본다. 이건 「버리는 길이 아예 없다」 를 본다 —
    둘이 필요한 이유는, 스위치를 기본 켜짐으로 두고 게이트를 넣으면 위 시험이
    통과하면서도 운영에서는 꺼진 값으로 버려질 수 있기 때문이다.

    버리는 것이 왜 안 되냐면, 안 쌓인 수정은 **다시 받을 수 없기** 때문이다.
    Slack 은 지난 `message_changed` 를 다시 주지 않는다. 막아야 한다면 버리는
    것이 아니라 **남기고 알리는** 쪽이어야 한다.
    """
    source = inspect.getsource(archiving_bot.ShadowCollector._ingest_revision)
    for name in UNENFORCED_FLAGS:
        assert name not in source, (
            f"수정·삭제 경로가 `{name}` 을 봅니다. 이 경로는 불변식이라 스위치로 "
            "끄지 않습니다 — 안 쌓인 수정은 Slack 에서 다시 받지 못합니다."
        )

    # 이벤트를 받고 나서 revision 경로로 **가기는 하는지**. 분기 자체가 사라지면
    # 위 검사는 통과하면서 수정·삭제가 통째로 안 들어온다.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    collector = _collector(tmp_path)
    assert collector.ingest_event(_Client(), _deleted_event()) == "revision-written"


# ---------------------------------------------------------------------------
# 불변식 2 — revision reader 는 **모르면 감춘다**
# ---------------------------------------------------------------------------


def _line(text: str, lineno: int, *, message_ts: str = TS) -> RawLine:
    return RawLine(
        ts="2026-09-22 18:40", speaker="U1", text=text, lineno=lineno,
        source_path=Path("raw/2026-09-22.md"), message_ts=message_ts,
    )


def test_a_marked_coordinate_is_hidden_when_the_record_cannot_be_read():
    """「지워졌는지 모른다」 와 「안 지워졌다」 는 다르다.

    모르는 채로 보여 주면 지운 메시지가 근거로 나갈 수 있고, 그건 되돌릴 수
    없다 — 사람은 이미 그 내용을 봤다.
    """
    lines = [
        _line("회의는 10시입니다", 1),
        _line("[수정 전] 회의는 10시입니다", 2),
        _line("[수정 후] 회의는 11시입니다", 3),
    ]

    kept = reader.visible_lines(
        lines, workspace=WS, channel_id=CH, lookup=lambda *_: reader.UNKNOWN
    )

    assert kept == []


def test_an_unmarked_coordinate_is_untouched_when_the_record_cannot_be_read():
    """감추는 범위는 **표시줄이 있는 좌표**뿐이다.

    여기가 넓어지면 DB 가 흔들릴 때마다 아카이브 전체가 답변에서 사라진다.
    사라진 것과 없는 것은 화면에서 같아 보이므로 아무도 눈치채지 못한다.
    """
    other = "1790070000.000002"
    lines = [
        _line("[수정 전] 회의는 10시입니다", 1),
        _line("예산은 3억입니다", 2, message_ts=other),
    ]

    kept = reader.visible_lines(
        lines, workspace=WS, channel_id=CH, lookup=lambda *_: reader.UNKNOWN
    )

    assert [line.text for line in kept] == ["예산은 3억입니다"]


def test_the_reader_runs_without_any_switch():
    """일반 조회는 **항상** reader 를 거친다.

    `docs()` 에 「거를지 말지」 인자가 생기면 호출부 하나가 기본값을 잘못 줘서
    지워진 문장이 답변에 나간다. 감사 조회는 `audit_docs()` 라는 **다른 함수**로
    갈라 두는 것이 그 실수를 구조로 막는 방법이다.
    """
    source = inspect.getsource(archive_store.ArchiveStore.docs)
    assert "revision_reader.apply" in source
    for name in UNENFORCED_FLAGS:
        assert name not in source

    taken = set(inspect.signature(archive_store.ArchiveStore.docs).parameters)
    assert taken == {"self", "dm_scope"}, (
        "docs() 가 받는 인자가 늘었습니다. 거르기를 끄는 인자가 생기면 "
        "호출부 하나가 기본값을 잘못 주는 날 지워진 문장이 답변에 나갑니다."
    )


def test_audit_reading_stays_a_separate_function():
    """과거 revision 은 **다른 문으로만** 나온다."""
    assert hasattr(archive_store.ArchiveStore, "audit_docs")
    source = inspect.getsource(archive_store.ArchiveStore.audit_docs)
    assert "revision_reader.apply" not in source


# ---------------------------------------------------------------------------
# 둘이 필수 스위치로 돌아오지 않는다
# ---------------------------------------------------------------------------


def test_the_invariants_are_not_listed_as_switches_again():
    for name in UNENFORCED_FLAGS:
        assert name not in REQUIRED_PRODUCTION_FLAGS


def test_the_reader_never_edits_the_file(tmp_path, monkeypatch):
    """거르는 것은 **읽을 때**다. 파일에서 줄을 지우지 않는다.

    지우면 감사에서 무엇이 바뀌었는지 못 본다. 그리고 지운 것은 되돌릴 수 없다 —
    원문은 한 번 사라지면 Slack 에서도 다시 못 받는다(원칙 1).
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    collector = _collector(tmp_path)
    assert collector.ingest_event(_Client(), _deleted_event()) == "revision-written"

    path = next((tmp_path / "shadow").glob("workspaces/*/channels/*/raw/*.md"))
    before = path.read_text(encoding="utf-8")
    assert "[삭제 전] 지울 문장" in before

    store = archive_store.ArchiveStore(tmp_path / "shadow")
    visible = [
        line.text
        for doc in store.docs()
        for line in doc.raw_lines
    ]

    # 답변 근거에서는 빠지고 —
    assert not [text for text in visible if "지울 문장" in text]
    # 파일은 **글자 하나까지 그대로다.**
    assert path.read_text(encoding="utf-8") == before
