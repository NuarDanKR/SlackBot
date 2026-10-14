"""수집 현황이 **Archiver 수집도 본다**(WC-19).

2026-10-14 오너 지적. `수집 > 수집 현황` 이 「조치 필요 · 수집 중단」 을 띄우는데
Archiver 는 멀쩡히 수집하고 있었다.

## 왜 그랬나

`reader.workspace_status` 는 운영 루트(`ARCHIVE_DIR`) 파일만 셌다. 그런데
Archiver 는 채널 모드에 따라 **두 곳**에 쓴다.

| 채널 모드 | 쓰는 곳 |
|---|---|
| `shadow` | `ARCHIVER_SHADOW_DIR` |
| `active` | `ARCHIVE_DIR` |

채널을 shadow 로 넘기면 운영 루트에는 새 줄이 안 쌓인다. 그러면 `_health` 가
`last_ingested` 를 오래됐다고 보고 `stalled` 을 낸다 — **전환 중에는 그게 정상인데
화면은 사고라고 말한다.** 그리고 그 빨간 줄을 눌러 가 봐야 고칠 것이 없다.

## 무엇을 하지 않는가

두 루트를 **합쳐 읽지 않는다.** 답변 근거의 단일 루트 원칙(2026-10-02 오너 결정)은
그대로다. 현황판이 두 곳을 **각각 세어 나란히** 보여 줄 뿐이다 — 합치면 인수
전후를 대조할 수 없다.
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from tybot.console import reader

KST = timezone(timedelta(hours=9))


class _Doc:
    """`ArchiveStore.docs()` 가 주는 것 중 현황판이 쓰는 칸만."""

    def __init__(self, workspace: str, *, last_ingested: str, lines: int = 3):
        self.workspace = workspace
        self.last_ingested = last_ingested
        self.raw_lines = [
            type("L", (), {"ts": f"{last_ingested[:10]} 09:0{index}"})()
            for index in range(lines)
        ]


class _Store:
    def __init__(self, root, docs):
        self.root = root
        self._docs = docs

    def docs(self):
        return self._docs

    def broken(self):
        return []


def _stamp(hours_ago: float) -> str:
    return (reader._now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def quiet(monkeypatch):
    """DB·하트비트 없이 현황판만 본다."""
    monkeypatch.setattr(reader, "_workspace_labels", lambda: {"tyit": "전산팀"})
    monkeypatch.setattr(reader, "_readable_map", lambda keys: {})
    monkeypatch.setattr(reader, "_root_keys", lambda: set())
    monkeypatch.setattr(reader, "_spend_by_workspace_today", lambda: {})
    monkeypatch.setattr(reader.heartbeat, "read", lambda key: None)
    monkeypatch.setattr(reader, "workspace_from_path", lambda path, root: "tyit")


def _row(rows):
    return next(row for row in rows if row["key"] == "tyit")


def test_a_workspace_collected_only_into_the_shadow_root_is_not_stalled(quiet, monkeypatch, tmp_path):
    """**Archiver 가 수집 중인데 「수집 중단」 이라고 하면 안 된다.**

    전환 중에는 운영 루트가 조용한 것이 정상이다. 그걸 사고로 표시하면 진짜
    사고와 구별되지 않고, 그 빨간 줄은 눌러도 고칠 것이 없다.
    """
    live = _Store(tmp_path / "archive", [])
    shadow = _Store(tmp_path / "shadow", [_Doc("tyit", last_ingested=_stamp(1))])
    monkeypatch.setattr(reader, "_shadow_store", lambda: shadow)

    row = _row(reader.workspace_status(live))

    assert row["health"] != "stalled"
    assert row["lastIngestedAt"] is not None


def test_the_two_roots_are_counted_separately(quiet, monkeypatch, tmp_path):
    """**합치지 않는다.** 합치면 인수 전후를 대조할 수 없다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(2), lines=5)])
    shadow = _Store(tmp_path / "shadow", [_Doc("tyit", last_ingested=_stamp(1), lines=7)])
    monkeypatch.setattr(reader, "_shadow_store", lambda: shadow)

    row = _row(reader.workspace_status(live))

    assert row["liveDocs"] == 1
    assert row["shadowDocs"] == 1
    assert row["liveRawLines"] == 5
    assert row["shadowRawLines"] == 7
    # 기존 칸은 운영 루트 그대로다 — 화면이 갑자기 다른 숫자를 보면 안 된다.
    assert row["docs"] == 1
    assert row["rawLines"] == 5


def test_each_root_reports_its_own_last_ingest(quiet, monkeypatch, tmp_path):
    """어느 쪽이 멈췄는지 알아야 인수 중 문제를 짚을 수 있다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(50))])
    shadow = _Store(tmp_path / "shadow", [_Doc("tyit", last_ingested=_stamp(1))])
    monkeypatch.setattr(reader, "_shadow_store", lambda: shadow)

    row = _row(reader.workspace_status(live))

    assert row["liveLastIngestedAt"] < row["shadowLastIngestedAt"]
    # 「수집이 돌고 있나」 는 둘 중 **새 쪽**이다.
    assert row["lastIngestedAt"] == row["shadowLastIngestedAt"]


def test_both_roots_quiet_is_still_stalled(quiet, monkeypatch, tmp_path):
    """**진짜 멈춘 것은 여전히 멈췄다고 한다.** 넓게 고쳐서 사고를 덮지 않는다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(100))])
    shadow = _Store(tmp_path / "shadow", [_Doc("tyit", last_ingested=_stamp(99))])
    monkeypatch.setattr(reader, "_shadow_store", lambda: shadow)

    assert _row(reader.workspace_status(live))["health"] == "stalled"


def test_no_shadow_root_configured_changes_nothing(quiet, monkeypatch, tmp_path):
    """그림자 루트를 안 쓰는 서버에서는 예전과 똑같아야 한다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(1))])
    monkeypatch.setattr(reader, "_shadow_store", lambda: None)

    row = _row(reader.workspace_status(live))

    assert row["shadowDocs"] == 0
    assert row["shadowLastIngestedAt"] is None
    assert row["health"] == "ok"


def test_an_unreadable_shadow_root_is_not_reported_as_empty(quiet, monkeypatch, tmp_path):
    """못 읽은 것과 없는 것은 다르다.

    0 으로 보여 주면 「그림자 수집이 안 되고 있다」 로 읽힌다 — 이 저장소가
    반복해서 데인 모양이다.
    """
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(1))])

    def boom():
        raise OSError("그림자 루트를 못 읽는다")

    monkeypatch.setattr(reader, "_shadow_store", boom)

    row = _row(reader.workspace_status(live))

    assert row["shadowDocs"] is None, "못 읽은 것을 0 으로 적었습니다"
    assert row["shadowError"]


def test_the_shadow_store_never_feeds_answer_evidence():
    """**현황판만 본다.** 근거 조회는 단일 루트 그대로다(2026-10-02 오너 결정)."""
    import inspect

    source = inspect.getsource(reader)
    counted = source.count("_shadow_store(")

    # 정의 1 + 현황판 1. 다른 곳에서 부르면 근거 경로로 샐 수 있다.
    assert counted <= 2, "그림자 루트를 현황판 밖에서도 읽습니다"


def test_a_workspace_that_exists_only_in_the_shadow_root_is_listed(monkeypatch, tmp_path):
    """그림자에만 있는 워크스페이스도 **보여야** 한다.

    설정에 없거나 운영 루트에 아직 아무것도 없는데 Archiver 가 그림자로 모으는
    중일 수 있다. 목록에서 빠지면 「수집이 안 되고 있다」 가 아니라 **아예
    존재하지 않는 것**으로 보인다 — 그러면 아무도 찾아보지 않는다.
    """
    monkeypatch.setattr(reader, "_workspace_labels", lambda: {})
    monkeypatch.setattr(reader, "_readable_map", lambda keys: {})
    monkeypatch.setattr(reader, "_root_keys", lambda: set())
    monkeypatch.setattr(reader, "_spend_by_workspace_today", lambda: {})
    monkeypatch.setattr(reader.heartbeat, "read", lambda key: None)
    monkeypatch.setattr(reader, "workspace_from_path", lambda path, root: "tyit")

    live = _Store(tmp_path / "archive", [])
    shadow = _Store(tmp_path / "shadow", [_Doc("tec", last_ingested=_stamp(1), lines=4)])
    monkeypatch.setattr(reader, "_shadow_store", lambda: shadow)

    rows = {row["key"]: row for row in reader.workspace_status(live)}

    assert "tec" in rows, "그림자에만 있는 워크스페이스가 목록에서 빠졌습니다"
    assert rows["tec"]["shadowRawLines"] == 4
    assert rows["tec"]["liveDocs"] == 0
    assert rows["tec"]["health"] != "stalled"
