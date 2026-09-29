"""소급이 **실시간과 같은 경로**로 쓴다.

결정: 2026-09-29 4단계 지시 2·4.

규칙을 복제하면 PII 검사·ACL·revision·ACK·첨부 처리가 두 벌이 되고, 한쪽만 고치는
날 **같은 대화가 들어온 길에 따라 다르게 남는다.** 그 차이는 오류를 내지 않는다.

여기서는 「같은 메서드를 부르는가」 와 「그 판정을 우회하지 않는가」 를 본다.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tybot.archive import backfill_adapter
from tybot.archive.backfill import Found, Target

ROOT = Path(__file__).resolve().parent.parent
BOT_SOURCE = (ROOT / "src" / "tybot" / "archiving_bot.py").read_text(encoding="utf-8")


class FakeCollector:
    def __init__(self, result: str = "written", resolved=("#팀_자금", False)) -> None:
        self.result = result
        self.resolved = resolved
        self.calls: list[dict] = []
        self.resolve_calls = 0

    def _collection_channel(self, _client, _channel_id):
        self.resolve_calls += 1
        return self.resolved

    def ingest_message(self, _client, event, *, channel, channel_id, is_private):
        self.calls.append({
            "event": event, "channel": channel, "channel_id": channel_id,
            "is_private": is_private,
        })
        return self.result


def _found(ts: str = "100.000100") -> Found:
    return Found("C1", ts, {"ts": ts, "user": "U1", "text": "사람이 쓴 줄"})


def _target() -> Target:
    return Target("tyit", "C1", "#팀_자금", "")


# --- 같은 경로를 쓴다 -----------------------------------------------------------

def test_the_adapter_calls_the_realtime_method():
    """이 시험이 이 모듈의 이유다. 다른 메서드를 부르면 규칙이 갈린다."""
    collector = FakeCollector()

    ingest = backfill_adapter.make_ingest(collector, object())
    result = ingest(_target(), _found())

    assert result == "written"
    assert collector.calls[0]["channel_id"] == "C1"
    assert collector.calls[0]["event"]["text"] == "사람이 쓴 줄"


def test_the_result_name_is_not_reclassified():
    """분류를 두 곳에 두면 한쪽이 「부분 성공」 을 성공으로 센다."""
    for name in ("written", "duplicate", "refused", "partial", "metadata-unconfirmed"):
        ingest = backfill_adapter.make_ingest(FakeCollector(name), object())

        assert ingest(_target(), _found()) == name


def test_the_private_flag_reaches_the_writer():
    """비공개 채널의 정본이 공개로 적히면 권한이 조용히 넓어진다."""
    collector = FakeCollector(resolved=("#비공개방", True))

    backfill_adapter.make_ingest(collector, object())(_target(), _found())

    assert collector.calls[0]["is_private"] is True


# --- 판정을 우회하지 않는다 -----------------------------------------------------

def test_a_channel_we_may_not_collect_is_refused():
    """끈 채널이 소급으로 되살아나면, 끈 사람은 그 사실을 모른다."""
    collector = FakeCollector(resolved=None)

    result = backfill_adapter.make_ingest(collector, object())(_target(), _found())

    assert result == "refused"
    assert collector.calls == []


def test_the_channel_is_resolved_once_per_run():
    """메시지마다 물으면 Slack 호출이 메시지 수만큼 늘고 rate limit 에 걸린다."""
    collector = FakeCollector()
    ingest = backfill_adapter.make_ingest(collector, object())

    for ts in ("100.000100", "200.000200", "300.000300"):
        ingest(_target(), _found(ts))

    assert collector.resolve_calls == 1
    assert len(collector.calls) == 3


# --- 대상 목록 -------------------------------------------------------------------

def test_targets_carry_the_history_cursor():
    """cursor 를 안 넘기면 매번 처음부터 읽고, 그건 재실행이 아니라 재수집이다."""
    rows = [{"channel_id": "C1", "channel_name": "팀_자금", "last_history_ts": "100.1"}]

    (target,) = backfill_adapter.targets_from(rows, "tyit")

    assert target.cursor == "100.1"
    assert target.channel == "#팀_자금"


def test_a_row_without_a_channel_id_is_skipped():
    assert backfill_adapter.targets_from([{"channel_name": "이름만"}], "tyit") == []


def test_a_channel_without_a_name_still_has_a_label():
    """이름이 없다고 목록에서 빠지면, 이름을 못 읽은 채널만 소급이 안 된다."""
    (target,) = backfill_adapter.targets_from([{"channel_id": "C9"}], "tyit")

    assert target.channel == "#C9"


# --- 규칙이 한 곳에만 있다 -------------------------------------------------------

def test_the_realtime_path_is_a_shared_method():
    """`ingest_event` 가 본문을 들고 있으면 소급이 그걸 복제하게 된다."""
    tree = ast.parse(BOT_SOURCE)
    collector = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ShadowCollector"
    )
    methods = {node.name for node in collector.body if isinstance(node, ast.FunctionDef)}

    assert "ingest_message" in methods


def test_the_event_path_delegates_to_it():
    event_body = BOT_SOURCE[BOT_SOURCE.index("    def ingest_event("):]
    event_body = event_body[:event_body.index("    def ingest_message(")]

    assert "return self.ingest_message(" in event_body
    # 원문 쓰기·첨부·revision 은 공통 메서드 몫이다. 여기 남아 있으면 복제다.
    assert "writer.ingest(" not in event_body
    assert "write_attachment_docs(" not in event_body


def test_the_adapter_does_not_reimplement_the_writer():
    source = (ROOT / "src" / "tybot" / "archive" / "backfill_adapter.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("writer.ingest", "write_attachment_docs", "stage_attachments",
                      "screen(", "IngestState"):
        assert forbidden not in source, forbidden
