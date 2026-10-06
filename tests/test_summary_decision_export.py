"""검토 결정 **내보내기와 대조의 실제 호출부** — helper 만 있으면 아무 일도 안 난다.

설계: `docs/design/summary-approval-ports.md` §3·§4

## 왜 호출부를 시험하나

`write_export()` 와 `filter_candidates()` 는 2026-10-05 에 들어왔지만 **부르는 곳이
없었다.** 그 상태에서도 단위 시험은 전부 통과한다 — 함수는 멀쩡하기 때문이다.
그래서 「대조가 붙었다」 로 읽히는데 운영에서는 결정 파일이 아예 안 생기고, 생기지
않으면 상대편은 늘 「끝낸 것이 없다」 를 본다.

여기의 ①~③ 이 그 상태를 금지한다. 나머지는 붙은 것이 **무엇을 쓰고 무엇을 안 쓰는가**
를 본다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tybot import summary_review_reconcile as rec

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "tybot"


# --- ① 실제 호출부가 있는가 ---------------------------------------------------

def _runtime_callers(symbol: str) -> dict[str, list[int]]:
    """`src/tybot/` 에서 그 이름을 **부르는** 자리. 정의한 모듈 자신은 뺀다."""
    out: dict[str, list[int]] = {}
    pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "summary_review_reconcile.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [i + 1 for i, line in enumerate(lines) if pattern.search(line)]
        if hits:
            out[path.relative_to(ROOT).as_posix()] = hits
    return out


def test_write_export_has_a_runtime_caller():
    """① 쓰는 쪽이 없으면 상대편은 영원히 「끝낸 것이 없다」 를 읽는다."""
    callers = _runtime_callers("write_export")
    assert callers, (
        "src/tybot/ 안에 write_export() 를 부르는 자리가 없습니다 — "
        "helper 시험만 통과하는 상태입니다."
    )


def test_the_candidate_filter_has_a_runtime_caller():
    """② 읽는 쪽이 없으면 상대편이 끝낸 검토를 또 묻는다."""
    assert _runtime_callers("filter_candidates"), (
        "src/tybot/ 안에 filter_candidates() 를 부르는 자리가 없습니다."
    )


def test_both_dm_decision_funnels_export():
    """③ DM 결정은 `settle` 과 `approve_all` **두 문**으로 들어온다.

    한 문만 내보내면 「전체 승인」 으로 끝낸 회차가 상대편에 안 보인다 — 그쪽은
    그 건을 다시 묻고, 사람은 같은 것을 두 번 본다.
    """
    body = (SRC / "summary_review.py").read_text(encoding="utf-8")
    for funnel in ("def settle(", "def approve_all(ack"):
        start = body.index(funnel)
        # 다음 핸들러 등록(`@app.`)까지가 그 함수의 몸이다.
        end = body.find("\n    @app.", start)
        chunk = body[start : end if end > start else len(body)]
        assert "export_decisions(" in chunk, f"{funnel} 에서 결정을 내보내지 않습니다."


# --- ② 출처별 파일 ------------------------------------------------------------

def test_each_source_writes_its_own_file(tmp_path):
    """④ 한 파일에 둘이 쓰면 마지막 쓰기가 앞 결정을 통째로 덮는다."""
    mine = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1")
    theirs = rec.export_path(tmp_path, source=rec.SOURCE_HERMES_INBOX, workspace="T1")
    assert mine != theirs
    assert mine.parent == theirs.parent


def test_the_path_separates_workspaces(tmp_path):
    """⑤ 다른 워크스페이스의 채널 구조를 같은 파일에 담지 않는다(원칙 4)."""
    a = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1")
    b = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T2")
    assert a.parent != b.parent


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "../etc", ".", "   "])
def test_a_path_shaped_name_is_refused(tmp_path, bad):
    """⑥ 워크스페이스 이름은 설정에서 온다 — 경로로 읽히면 저장소 밖에 쓴다."""
    with pytest.raises(rec.UnsafeExportTarget):
        rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace=bad)


# --- ③ 상대편 기록 읽기 ------------------------------------------------------

def _decision(**over) -> dict:
    row = {
        "workspace": "T1", "channel_id": "C1",
        "evidence_locator": "2026-10-01.md:12", "evidence_hash": "abc123",
        "state": "approved",
    }
    row.update(over)
    return row


def _write(path: Path, rows, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": rec.EXPORT_SCHEMA, "source": source, "decisions": rows},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def test_the_counterpart_file_is_read_and_my_own_is_not(tmp_path):
    """⑦ 내가 쓴 결정을 내가 다시 읽어 생략하면 **DM 이 자기 결정으로 자기를 막는다.**"""
    _write(rec.export_path(tmp_path, source=rec.SOURCE_HERMES_INBOX, workspace="T1"),
           [_decision()], rec.SOURCE_HERMES_INBOX)
    _write(rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1"),
           [_decision(evidence_locator="mine.md:1")], rec.SOURCE_TYBOT_DM)

    rows = rec.counterpart_decisions(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1")

    assert [d.evidence_locator for d in rows] == ["2026-10-01.md:12"]


def test_one_unreadable_counterpart_file_blocks_every_skip(tmp_path):
    """⑧ 못 읽은 파일이 하나라도 있으면 **전부 다시 묻는다.**

    읽은 것만으로 판정하면, 상대편의 「거절」 기록이 깨진 날 그 건이 「결정 없음」
    으로 보여 생략 판정이 다른 파일의 승인으로 넘어갈 수 있다.
    """
    _write(rec.export_path(tmp_path, source=rec.SOURCE_HERMES_INBOX, workspace="T1"),
           [_decision()], rec.SOURCE_HERMES_INBOX)
    broken = rec.export_path(tmp_path, source="other-tool", workspace="T1")
    broken.write_text("{not json", encoding="utf-8")

    assert rec.counterpart_decisions(
        tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1") is None


def test_no_counterpart_directory_is_read_as_nothing_decided(tmp_path):
    """⑨ 아직 아무도 안 쓴 것은 「못 읽음」 이 아니다 — 그러면 운영이 안 시작된다."""
    assert rec.counterpart_decisions(
        tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1") == []


# --- ④ 권위 DB 전체 스냅샷 ---------------------------------------------------

class _FakeStore:
    """`decision_snapshot` 만 가진 가짜 store. DB 없이 호출부를 잰다."""

    def __init__(self, rows):
        self.rows = rows
        self.asked: list[str] = []

    def decision_snapshot(self, workspace: str):
        self.asked.append(workspace)
        return [r for r in self.rows if r.get("workspace") == workspace]


def test_export_decisions_writes_the_whole_snapshot(tmp_path):
    """⑩ 부분 갱신이 아니라 **통째로** 쓴다 — 취소된 결정이 파일에 남으면 안 된다."""
    from tybot.summary_review import export_decisions

    store = _FakeStore([
        _decision(id="1"),
        _decision(id="2", state="rejected", evidence_locator="x.md:3"),
        _decision(id="3", state="deferred", evidence_locator="y.md:4"),
        _decision(id="4", workspace="T2"),
    ])

    written = export_decisions(store, "T1", root=tmp_path)

    assert written == 3
    path = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM, workspace="T1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == rec.SOURCE_TYBOT_DM
    assert {d["state"] for d in payload["decisions"]} == {"approved", "rejected", "deferred"}


def test_export_decisions_is_a_no_op_without_a_configured_root(monkeypatch):
    """⑪ 경로를 안 정한 설치에서 결정이 **실패하면 안 된다.**

    내보내기는 부가 기능이다. 여기서 던지면 승인 버튼이 「처리 실패」 를 돌려주고,
    사람은 이미 DB 에 들어간 결정을 다시 누른다.
    """
    from tybot.summary_review import export_decisions

    monkeypatch.delenv(rec.EXPORT_DIR_ENV, raising=False)
    store = _FakeStore([_decision()])

    assert export_decisions(store, "T1") == 0
    assert store.asked == []


def test_a_failed_export_does_not_undo_the_decision(tmp_path, monkeypatch):
    """⑫ 파일을 못 써도 결정은 유지된다 — 예외가 핸들러까지 올라오면 안 된다."""
    from tybot.summary_review import export_decisions

    def boom(*a, **k):
        raise OSError("디스크 가득")

    monkeypatch.setattr(rec, "write_export", boom)
    assert export_decisions(_FakeStore([_decision()]), "T1", root=tmp_path) == 0


def test_the_snapshot_carries_no_summary_text(tmp_path):
    """⑬ 본문·인용·Slack 원문은 싣지 않는다(원칙 4 — 크로스 워크스페이스 노출)."""
    from tybot.summary_review import export_decisions

    store = _FakeStore([_decision(
        id="1", proposed_text="자금 집행 12억", evidence_quote="12억 집행했습니다",
        current_text="자금 집행 9억", correction="사실은 11억",
    )])
    export_decisions(store, "T1", root=tmp_path)

    raw = rec.export_path(tmp_path, source=rec.SOURCE_TYBOT_DM,
                          workspace="T1").read_text(encoding="utf-8")
    for leaked in ("12억", "자금 집행", "사실은 11억", "집행했습니다"):
        assert leaked not in raw, f"결정 파일에 본문이 실렸습니다: {leaked}"
