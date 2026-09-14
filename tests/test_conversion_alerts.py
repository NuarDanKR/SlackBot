"""첨부 변환 알림 — 채널 단위 묶음 (B-45 §6).

설계: `docs/design/operational-warning-recovery-and-answer-progress.md`

이 파일이 지키는 것 셋.

1. **검토를 막을 때만 알린다.** 큐가 다시 해 볼 것이 남았으면 안 알린다 —
   곧 성공할 일로 부르면 사람은 그 DM 을 안 읽게 되고, 정작 중요한 한 건도 묻힌다.
2. **채널 단위로 묶는다.** 파일마다 보내면 20건 실패한 날 DM 이 20개 간다.
3. **파일명·본문을 기록에 남기지 않는다.** 알림 기록이 새 유출 경로가 되면 안 된다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tybot import conversion_alerts as alerts
from tybot.attachment_review import Attachment

SCHEMA = (
    Path(__file__).resolve().parent.parent / "deploy" / "sql" / "conversion_alert_schema.sql"
)


def _item(**kw) -> Attachment:
    base = dict(
        workspace="pilot", channel_id="C1", file_id="F1", name="정산.pdf",
        filetype="pdf", mimetype="application/pdf", size=1, status="converted",
        object_path=None, meta_path=Path("m.json"),
    )
    base.update(kw)
    return Attachment(**base)


# =============================================================================
# 언제 알리나
# =============================================================================


def test_a_retryable_failure_is_not_announced():
    """큐가 몇 분 뒤 성공할 수 있다. 그때마다 DM 이 가면 아무도 안 읽는다."""
    item = _item(
        status="download_or_extract_failed", error_code="converter_timeout", retryable=True
    )

    assert alerts.alert_state(item) == ""


def test_a_final_failure_is_announced():
    item = _item(status="download_or_extract_failed", error_code="corrupt", retryable=False)

    assert alerts.alert_state(item) == "failed"


def test_a_policy_exclusion_is_never_announced():
    """정책 제외는 고칠 것이 없다. 알려도 사람이 할 일이 없다.

    **부분 변환 뒤에 차단된 경우**를 본다. 그냥 `pii_refused` 만으로는 어차피
    실패 상태가 아니라서 이 분기를 지나지 않는다 — 되돌림 실험에서 드러났다.
    검사가 실제 분기를 안 지나면 그 검사는 아무것도 보장하지 않는다.
    """
    item = _item(
        status="pii_refused", error_code="pii_refused", conversion_state="partial"
    )

    assert alerts.alert_state(item) == ""


def test_a_partial_conversion_is_announced_because_the_answer_still_goes_out():
    """실패와 다르다 — **답이 나간다.** 범위를 모르면 전부 본 줄 안다."""
    item = _item(status="converted", conversion_state="partial", extracted=True)

    assert alerts.alert_state(item) == "partial"


def test_a_healthy_attachment_is_not_announced():
    assert alerts.alert_state(_item(extracted=True, conversion_state="succeeded")) == ""


# =============================================================================
# 채널 단위 묶음
# =============================================================================


def _staged(tmp_path, file_id, *, status, error_code="", retryable=False,
            conversion_state="", channel="C1") -> None:
    from tybot.attachment_review import staging_root

    d = (
        staging_root(tmp_path / "archive") / "pilot" / "channels" / channel
        / "attachments" / file_id
    )
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        json.dumps({
            "name": f"{file_id}.pdf", "filetype": "pdf", "status": status,
            "error_code": error_code, "retryable": retryable,
            "conversion_state": conversion_state, "extracted": status == "converted",
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def test_three_failures_in_one_channel_become_one_alert(tmp_path):
    for fid in ("F1", "F2", "F3"):
        _staged(tmp_path, fid, status="download_or_extract_failed", error_code="corrupt")

    got = alerts.collect(tmp_path / "archive")

    assert len(got) == 1
    assert len(got[0].items) == 3


def test_two_channels_become_two_alerts(tmp_path):
    _staged(tmp_path, "F1", status="download_or_extract_failed", error_code="corrupt")
    _staged(tmp_path, "F2", status="download_or_extract_failed", error_code="corrupt",
            channel="C2")

    got = alerts.collect(tmp_path / "archive")

    assert {a.channel_id for a in got} == {"C1", "C2"}


def test_a_queued_job_holds_back_the_alert(tmp_path):
    """metadata 만으로는 「아직 재시도 중」 을 알 수 없다. 큐가 말해 준다."""
    _staged(tmp_path, "F1", status="download_or_extract_failed", error_code="corrupt")

    got = alerts.collect(tmp_path / "archive", queue_state=lambda ws, ch, f: "queued")

    assert got == []


def test_a_held_job_is_announced_as_held(tmp_path):
    """`held` 는 **환경** 문제다. `failed` 와 사람이 할 일이 다르다."""
    _staged(tmp_path, "F1", status="download_or_extract_failed",
            error_code="converter_missing")

    (alert,) = alerts.collect(tmp_path / "archive", queue_state=lambda ws, ch, f: "held")

    assert alert.items[0].state == "held"
    assert "관리자" in alert.items[0].next_step


def test_a_queue_outage_does_not_stop_alerts(tmp_path, caplog):
    """큐를 못 읽는다고 알림 전체가 멈추면, 정작 급한 건도 안 나간다."""
    _staged(tmp_path, "F1", status="download_or_extract_failed", error_code="corrupt")

    def boom(ws, ch, f):
        raise RuntimeError("DB 죽음")

    with caplog.at_level("WARNING"):
        got = alerts.collect(tmp_path / "archive", queue_state=boom)

    assert len(got) == 1


# =============================================================================
# 멱등
# =============================================================================


def test_the_same_state_makes_the_same_key():
    a = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "a.pdf", "failed", "corrupt"),
    ))
    b = alerts.ChannelAlert("pilot", "C1", "#이름이바뀜", (
        alerts.AlertItem("F1", "다른이름.pdf", "failed", "corrupt"),
    ))

    assert a.dedupe_key == b.dedupe_key, "이름이 바뀌어도 같은 건이다"


def test_a_changed_state_makes_a_new_key():
    """실패가 부분 성공이 되면 다시 알릴 값이 있다."""
    a = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "a.pdf", "failed", "corrupt"),
    ))
    b = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "a.pdf", "partial", ""),
    ))

    assert a.dedupe_key != b.dedupe_key


def test_a_new_file_makes_a_new_key():
    a = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "a.pdf", "failed", "corrupt"),
    ))
    b = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "a.pdf", "failed", "corrupt"),
        alerts.AlertItem("F2", "b.pdf", "failed", "corrupt"),
    ))

    assert a.dedupe_key != b.dedupe_key


# =============================================================================
# 권한
# =============================================================================


def test_a_user_who_left_the_channel_is_not_told():
    """검토자로 등록된 뒤 채널에서 빠졌을 수 있다. 파일명을 보내면 그게 유출이다."""
    client = SimpleNamespace(conversations_members=lambda **kw: {"members": ["U-OTHER"]})

    assert alerts.may_see_channel(client, "C1", "U-GONE") is False


def test_a_member_is_told():
    client = SimpleNamespace(conversations_members=lambda **kw: {"members": ["U1", "U2"]})

    assert alerts.may_see_channel(client, "C1", "U1") is True


def test_a_membership_lookup_failure_blocks_the_send():
    """확인할 수 없으면 보내지 않는다 — 막는 쪽이 기본값(원칙 3)."""

    def boom(**kw):
        raise RuntimeError("Slack 장애")

    client = SimpleNamespace(conversations_members=boom)

    assert alerts.may_see_channel(client, "C1", "U1") is False


# =============================================================================
# 무엇을 담나
# =============================================================================


def test_the_message_carries_a_next_step_for_each_code():
    alert = alerts.ChannelAlert("pilot", "C1", "#현장-광주", (
        alerts.AlertItem("F1", "정산.pdf", "failed", "encrypted"),
    ))

    text = alert.text()

    assert "정산.pdf" in text
    assert "다음 조치" in text
    assert "암호" in text, "무엇을 해야 하는지 말해야 한다"


def test_a_missing_permalink_is_not_invented():
    """없는 링크를 만들어 내면 사람이 눌렀을 때 아무 데도 안 간다."""
    line = alerts.AlertItem("F1", "정산.pdf", "failed", "corrupt").line()

    assert "정산.pdf" in line
    assert "http" not in line


def test_a_partial_item_shows_how_much_was_read():
    line = alerts.AlertItem(
        "F1", "정산.pdf", "partial", "", coverage_note="확인 3/10쪽"
    ).line()

    assert "확인 3/10쪽" in line
    assert "일부만 읽음" in line


def test_a_long_list_is_capped_with_a_count():
    """200줄짜리 DM 은 아무도 안 읽는다."""
    items = tuple(
        alerts.AlertItem(f"F{i}", f"파일{i}.pdf", "failed", "corrupt") for i in range(40)
    )
    text = alerts.ChannelAlert("pilot", "C1", "#현장", items).text()

    assert "외 " in text
    assert text.count("•") <= alerts.MAX_FILES_IN_MESSAGE + 5


def test_the_log_line_has_no_file_names():
    alert = alerts.ChannelAlert("pilot", "C1", "#현장", (
        alerts.AlertItem("F1", "광주도시철도_미수금.pdf", "failed", "corrupt"),
    ))

    line = alert.log_line()

    assert "미수금" not in line
    assert "files=1" in line


def test_the_sent_table_stores_no_business_text():
    """알림 기록이 업무 내용의 사본이 되면 그 표가 새 유출 경로다."""
    sql = SCHEMA.read_text(encoding="utf-8").lower()

    for forbidden in ("name text", "file_name", "body", "content", "payload"):
        assert forbidden not in sql, f"{forbidden} 열이 생겼다"
    assert "grant" in sql, "표만 만들고 GRANT 를 안 주면 봇에게는 없는 것과 같다"


# =============================================================================
# 수신자
# =============================================================================


def test_recipients_come_from_stored_ids_not_display_names(monkeypatch):
    """같은 이름이 둘이면 남에게 간다."""
    from tybot import reviewers as reviewers_mod

    monkeypatch.setattr(
        reviewers_mod, "reviewers_for",
        lambda ws, ch: [SimpleNamespace(reviewer_user="U-REVIEWER", enabled=True)],
    )

    got = alerts.recipients("pilot", "C1", owner_of=lambda ws, ch: "U-OWNER")

    assert got == ["U-REVIEWER", "U-OWNER"]


def test_one_side_failing_still_reaches_the_other(monkeypatch, caplog):
    from tybot import reviewers as reviewers_mod

    def boom(ws, ch):
        raise RuntimeError("검토자 표 장애")

    monkeypatch.setattr(reviewers_mod, "reviewers_for", boom)

    with caplog.at_level("WARNING"):
        got = alerts.recipients("pilot", "C1", owner_of=lambda ws, ch: "U-OWNER")

    assert got == ["U-OWNER"]


def test_a_duplicate_recipient_is_told_once(monkeypatch):
    from tybot import reviewers as reviewers_mod

    monkeypatch.setattr(
        reviewers_mod, "reviewers_for",
        lambda ws, ch: [SimpleNamespace(reviewer_user="U1", enabled=True)],
    )

    assert alerts.recipients("pilot", "C1", owner_of=lambda ws, ch: "U1") == ["U1"]
