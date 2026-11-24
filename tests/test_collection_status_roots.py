"""수집 현황은 **운영 루트 하나만** 센다(B-99 2단계 D1·D6).

## 내력

2026-10-14(WC-19)에는 Archiver 가 채널 모드에 따라 두 곳에 썼다 — `shadow` 면
`ARCHIVER_SHADOW_DIR`, `active` 면 `ARCHIVE_DIR`. 그래서 현황판이 그림자 루트를
따로 세어 나란히 보여 줬다.

B-99 2단계에서 그림자 수집을 폐기했다. Archiver 는 `ARCHIVE_DIR` 에만 쓰고, 서버에
남은 섀도 루트 데이터는 **보관분**이다. 보관분을 「수집이 돌고 있나」 의 근거로 세면
멈춘 워크스페이스가 옛 파일 덕에 살아 있는 것처럼 보인다.

## 이 시험이 고정하는 것

- `ARCHIVER_SHADOW_DIR` 가 남아 있어도 현황판은 읽지 않는다
- 응답에 `shadow*` 칸이 없다 — 화면이 없는 숫자를 그리지 않게
- 운영 루트가 조용하면 여전히 「수집 중단」 이다
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


def test_the_status_counts_the_live_root_only(quiet, tmp_path):
    """운영 루트의 문서·줄·마지막 수집 시각이 그대로 나온다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(1), lines=5)])

    row = _row(reader.workspace_status(live))

    assert row["docs"] == 1
    assert row["rawLines"] == 5
    assert row["liveDocs"] == 1
    assert row["liveRawLines"] == 5
    assert row["lastIngestedAt"] == row["liveLastIngestedAt"]
    assert row["health"] == "ok"


def test_no_shadow_fields_are_reported(quiet, tmp_path):
    """그림자 루트 집계를 없앴다. 칸이 남으면 화면이 없는 숫자를 그린다."""
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(1))])

    row = _row(reader.workspace_status(live))

    assert not [key for key in row if key.lower().startswith("shadow")]


def test_a_leftover_shadow_dir_setting_is_ignored(quiet, monkeypatch, tmp_path):
    """**보관분은 수집 증거가 아니다.** 섀도 루트에 최근 파일이 있어도 운영 루트가
    조용하면 멈춘 것이다."""
    shadow_root = tmp_path / "shadow"
    shadow_root.mkdir()
    monkeypatch.setenv("ARCHIVER_SHADOW_DIR", str(shadow_root))
    live = _Store(tmp_path / "archive", [_Doc("tyit", last_ingested=_stamp(100))])

    row = _row(reader.workspace_status(live))

    assert row["health"] == "stalled"
    assert row["lastIngestedAt"] == row["liveLastIngestedAt"]


def test_the_reader_has_no_shadow_root_path():
    """현황판이 섀도 루트를 여는 길 자체가 없다."""
    import inspect

    source = inspect.getsource(reader)

    assert "ARCHIVER_SHADOW_DIR" not in source
    assert not hasattr(reader, "_shadow_store")
    assert not hasattr(reader, "_shadow_rollup")
