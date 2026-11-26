"""B-100 — 이미지 처리 상태를 원본 스레드의 댓글 **하나**로 보여 준다.

거는 것:
- 접수 → 처리 중 → 완료: `chat.postMessage` 1회 뒤 같은 `ts` 에 `chat.update` 만(댓글 1개)
- 단계별 문장: `Vision LLM` 과 `기본 OCR` 을 가르고, 성공 전에는 「인식 완료」 가 없다.
  `uncertain` 은 「확인 필요」. 「검색 반영: 완료」 는 검색 투영이 확인된 뒤에만
- 같은 단계를 다시 적어도(재시작·중복 행·같은 job 재실행) 댓글이 늘지 않는다
- 늦게 온 옛 상태가 새 상태를 덮지 않는다. 댓글이 지워졌을 때만 새로 달고 `ts` 를 바꾼다
- Slack 일시 오류는 재시도해 성공하고, 등록 해제·멤버 아님은 `failed_delivery` 로 남는다
- 게시 뒤 `reply_ts` 를 못 적고 죽어도 다음 시도가 스레드에서 찾아 같은 댓글을 고친다
- 민감정보는 가려지고 Slack 제어 문자는 막힌다. 오류에는 예외 본문이 실리지 않는다
- worker 기록 실패가 Vision 결과·호출 수를 바꾸지 않는다
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tybot.archive import image_status_notice as notices
from tybot.archive.image_description_coordinator import ImageDescriptionBatchItem

WS = "tyit"
CH = "C12345678"
TS = "1794000000.000100"
FILE = "F12345678"
KEY = notices.NoticeKey(WS, CH, TS, FILE)
JOB_A = "a" * 32
JOB_B = "b" * 32
T0 = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
LEAK = "SECRET-VALUE-MUST-NOT-LEAK"


class SlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(f"Slack said {error} with {LEAK}")
        self.response = {"error": error}


class FakeSlack:
    """스레드 하나를 흉내 낸다. 게시·수정을 세고 실패를 주입한다."""

    def __init__(self, *, member: bool = True) -> None:
        self.member = member
        self.thread: dict[str, dict] = {}
        self.posts = 0
        self.updates: list[str] = []
        self.fail_next: list[str] = []
        self.counter = 0

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise SlackError(self.fail_next.pop(0))

    def conversations_info(self, *, channel):
        return {"channel": {"id": channel, "is_member": self.member}}

    def chat_postMessage(self, *, channel, thread_ts, text, metadata, **_kw):
        self._maybe_fail()
        assert thread_ts == TS
        self.posts += 1
        self.counter += 1
        ts = f"1794000001.{self.counter:06d}"
        self.thread[ts] = {"ts": ts, "text": text, "metadata": metadata, "user": "UBOT"}
        return {"ts": ts}

    def chat_update(self, *, channel, ts, text, metadata):
        self._maybe_fail()
        if ts not in self.thread:
            raise SlackError("message_not_found")
        self.thread[ts]["text"] = text
        self.updates.append(ts)
        return {"ts": ts}

    def conversations_replies(self, *, channel, ts, include_all_metadata, limit):
        assert include_all_metadata is True
        return {"messages": list(self.thread.values())}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _repo(clock: Clock | None = None) -> notices.MemoryNoticeRepo:
    return notices.MemoryNoticeRepo(now=clock or Clock())


def _deliver(slack, repo, clock=None, channels=frozenset({CH})):
    return notices.deliver_pending(
        slack, repo, WS, allowed_channels=channels, bot_user_id="UBOT", now=clock
    )


def _accepted(**payload) -> notices.NoticeRow:
    base = notices.receipt_payload(
        name="현장사진.png", conversion_state="succeeded", has_text=True, vision_enabled=True
    )
    return notices.NoticeRow(key=KEY, phase=notices.ACCEPTED, payload=base | payload)


def _row(phase, job="", created=None, **payload) -> notices.NoticeRow:
    return notices.NoticeRow(
        key=KEY, phase=phase, job_id=job, job_created_at=created, payload=payload
    )


def _success(**over) -> dict:
    return {
        "description": "콘크리트 타설 현장",
        "tags": ["현장", "콘크리트"],
        "transcript": ["안전제일", "출입 시 안전모 착용"],
        "transcript_more": 0,
        "provider": "google",
        "search_published": True,
    } | over


# --- 한 댓글을 고쳐 간다 ------------------------------------------------------------


def test_receipt_processing_completed_is_one_post_then_updates_only():
    repo, slack = _repo(), FakeSlack()

    repo.record(_accepted())
    _deliver(slack, repo)
    repo.record(_row(notices.PROCESSING, JOB_A, T0))
    _deliver(slack, repo)
    repo.record(_row(notices.COMPLETED, JOB_A, T0, **_success()))
    _deliver(slack, repo)

    assert slack.posts == 1
    assert len(slack.thread) == 1
    (reply,) = slack.thread.values()
    assert slack.updates == [reply["ts"], reply["ts"]]
    assert reply["text"].startswith("✅ 이미지 인식 완료")
    row = repo.rows[KEY]
    assert row.delivery_state == "delivered" and row.delivered_version == row.version == 3
    assert row.reply_ts == reply["ts"]
    # 댓글의 metadata 는 좌표 해시뿐이다 — 본문·파일명을 싣지 않는다.
    assert reply["metadata"] == {
        "event_type": notices.METADATA_EVENT,
        "event_payload": {"key": KEY.digest},
    }


def test_the_same_phase_recorded_again_sends_nothing_more():
    """worker 재시작·중복 행·같은 job 재실행·Slack 재전달."""
    repo, slack = _repo(), FakeSlack()
    assert repo.record(_accepted()) is True
    assert repo.record(_accepted()) is False
    _deliver(slack, repo)
    assert repo.record(_row(notices.PROCESSING, JOB_A, T0)) is True
    assert repo.record(_row(notices.PROCESSING, JOB_A, T0)) is False
    _deliver(slack, repo)
    _deliver(slack, repo)

    assert slack.posts == 1 and len(slack.updates) == 1
    assert repo.rows[KEY].version == 2


def test_multiple_images_in_one_message_each_get_their_own_named_comment():
    repo, slack = _repo(), FakeSlack()
    other = notices.NoticeKey(WS, CH, TS, "F99999999")
    repo.record(_accepted())
    repo.record(
        notices.NoticeRow(
            key=other,
            phase=notices.ACCEPTED,
            payload=notices.receipt_payload(
                name="도면.jpg",
                conversion_state="succeeded",
                has_text=False,
                vision_enabled=True,
            ),
        )
    )
    _deliver(slack, repo)

    texts = [message["text"] for message in slack.thread.values()]
    assert len(texts) == 2
    assert any("- 파일: 현장사진.png" in text for text in texts)
    assert any("- 파일: 도면.jpg" in text for text in texts)


# --- 단계별 문장 -----------------------------------------------------------------


def test_receipt_separates_basic_ocr_and_vision():
    text = notices.render(notices.ACCEPTED, _accepted().payload)
    assert text.splitlines() == [
        "✅ 이미지 접수 완료",
        "- 파일: 현장사진.png",
        "- 기본 OCR: 보관 완료",
        "- Vision LLM: 처리 대기",
    ]
    off = notices.render(notices.ACCEPTED, _accepted(vision_enabled=False).payload)
    assert "- Vision LLM: 사용 안 함 (기본 OCR 자료만 씁니다)" in off


@pytest.mark.parametrize(
    "phase", [notices.ACCEPTED, notices.PROCESSING, notices.DEFERRED, notices.FAILED]
)
def test_nothing_says_recognition_finished_before_vision_succeeds(phase):
    text = notices.render(phase, _accepted().payload | {"reason": "provider_error"})
    assert "인식 완료" not in text and "검색 반영" not in text


def test_processing_text():
    text = notices.render(notices.PROCESSING, _accepted().payload)
    assert text.splitlines()[0] == "⏳ 이미지 인식 처리 중"
    assert "- Vision LLM: 처리 중" in text


def test_completed_names_the_vision_llm_and_search_publication():
    text = notices.render(notices.COMPLETED, _accepted().payload | _success())
    lines = text.splitlines()
    assert lines[0] == "✅ 이미지 인식 완료"
    assert "- 처리 방식: Google Vision LLM" in lines
    assert "- 텍스트 인식: 완료" in lines
    assert "- 이미지 설명: 완료" in lines
    assert "- 검색 반영: 완료" in lines
    assert "│ 안전제일" in lines
    assert "_AI 인식 결과입니다. 원본 이미지를 확인해 주세요._" in lines


def test_search_publication_is_not_claimed_until_confirmed():
    text = notices.render(notices.COMPLETED, _success(search_published=False))
    assert "- 검색 반영: 확인 중" in text
    assert "- 검색 반영: 완료" not in text


def test_uncertain_is_not_dressed_up_as_success():
    text = notices.render(notices.UNCERTAIN, _success())
    assert text.splitlines()[0] == "🟡 이미지 인식 완료 · 확인 필요"
    assert "불확실" in text
    assert not text.startswith("✅")


def test_deferred_text_says_basic_ocr_is_in_use_and_retry_is_scheduled():
    text = notices.render(notices.DEFERRED, {"file": "a.png", "reason": "provider_unavailable"})
    assert text.splitlines() == [
        "⚠️ Vision LLM 처리 보류",
        "- 파일: a.png",
        "- 현재 사용 자료: 기본 OCR",
        "- 상태: 외부 모델 일시 오류로 다음 배치에서 재시도합니다",
    ]
    limited = notices.render(notices.DEFERRED, {"reason": "limit"})
    assert "호출 한도" in limited


def test_a_dispatched_provider_failure_does_not_promise_an_automatic_retry():
    """한 번 나간 job 은 중복 과금을 막으려고 같은 원본을 다시 보내지 않는다(duplicate_job)."""
    text = notices.render(notices.FAILED, {"reason": "provider_error"})
    assert "다음 배치에서 재시도" not in text
    assert "자동으로 다시 보내지 않습니다" in text
    assert "- 현재 사용 자료: 기본 OCR" in text


def test_long_transcript_is_capped_and_counted():
    payload = _success(transcript=[f"줄{i}" for i in range(12)], transcript_more=30)
    text = notices.render(notices.COMPLETED, payload)
    assert text.count("│ 줄") == 12
    assert "│ …추가 30줄은 검색에 반영됨" in text


def test_sensitive_values_are_masked_and_slack_controls_escaped():
    payload = {
        "file": notices.file_label("<@U123> 010-1234-5678.png"),
        "description": "<!channel> 연락처 010-9999-8888",
        "transcript": ["<https://evil.example|클릭>"],
        "provider": "google",
        "search_published": True,
    }
    text = notices.render(notices.COMPLETED, payload)
    assert "010-1234-5678" not in text and "010-9999-8888" not in text
    assert "<@" not in text and "<!channel>" not in text and "<https" not in text
    assert "&lt;" in text


def test_file_label_is_masked_clipped_and_dropped_when_unmaskable():
    assert notices.file_label("a" * 200 + ".png").endswith("…")
    assert len(notices.file_label("a" * 200)) <= notices.MAX_FILE_LABEL
    assert "900101-1234567" not in notices.file_label("주민 900101-1234567.png")


# --- 전진 규칙 -------------------------------------------------------------------


def test_a_late_receipt_keeps_the_result_and_only_marks_it_for_slack():
    """worker 완료가 접수보다 먼저 적혔다(경합). 완료를 잃지 않고 댓글 대상만 켠다."""
    repo, slack = _repo(), FakeSlack()
    repo.record(_row(notices.COMPLETED, JOB_A, T0, **_success()))
    assert repo.pending(WS) == []  # 접수 전에는 Slack 대상이 아니다

    assert repo.record(_accepted()) is True
    row = repo.rows[KEY]
    assert row.phase == notices.COMPLETED and row.slack_eligible is True
    assert row.payload["file"] == "현장사진.png"  # 빈 칸만 접수에서 채웠다
    assert row.payload["description"] == "콘크리트 타설 현장"
    assert repo.record(_accepted()) is False  # 재전달은 아무것도 바꾸지 않는다

    _deliver(slack, repo)
    (reply,) = slack.thread.values()
    assert reply["text"].startswith("✅ 이미지 인식 완료")


def test_a_late_processing_of_the_same_job_does_not_overwrite_completion():
    repo = _repo()
    repo.record(_row(notices.PROCESSING, JOB_A, T0))
    repo.record(_row(notices.COMPLETED, JOB_A, T0, **_success()))
    assert repo.record(_row(notices.PROCESSING, JOB_A, T0)) is False
    assert repo.record(_row(notices.FAILED, JOB_A, T0, reason="blocked")) is False
    assert repo.rows[KEY].phase == notices.COMPLETED


def test_an_older_job_cannot_overwrite_a_newer_one_but_a_newer_one_can():
    repo = _repo()
    repo.record(_row(notices.COMPLETED, JOB_B, T0 + timedelta(hours=1), **_success()))
    assert repo.record(_row(notices.FAILED, JOB_A, T0, reason="provider_error")) is False
    newer = "c" * 32
    assert repo.record(_row(notices.PROCESSING, newer, T0 + timedelta(hours=2))) is True
    assert repo.rows[KEY].phase == notices.PROCESSING


def test_a_retry_without_a_job_never_hides_a_success():
    repo = _repo()
    repo.record(_row(notices.COMPLETED, JOB_A, T0, **_success()))
    assert repo.record(_row(notices.DEFERRED, reason="provider_unavailable")) is False


def test_a_retry_after_a_failed_job_is_shown_once():
    repo = _repo()
    repo.record(_row(notices.FAILED, JOB_A, T0, reason="provider_error"))
    assert repo.record(_row(notices.DEFERRED, reason="provider_unavailable")) is True
    assert repo.record(_row(notices.DEFERRED, reason="provider_unavailable")) is False


def test_later_phases_keep_the_file_name_from_the_receipt():
    repo = _repo()
    repo.record(_accepted())
    repo.record(_row(notices.COMPLETED, JOB_A, T0, **_success()))
    assert "- 파일: 현장사진.png" in notices.render(repo.rows[KEY].phase, repo.rows[KEY].payload)
    repo.record(_row(notices.PROCESSING, JOB_B, T0 + timedelta(hours=1)))
    assert "description" not in repo.rows[KEY].payload  # 옛 결과를 새 처리에 섞지 않는다


# --- Slack 전달 -------------------------------------------------------------------


def test_a_transient_slack_error_is_retried_and_then_succeeds():
    clock, slack = Clock(), FakeSlack()
    repo = _repo(clock)
    repo.record(_accepted())
    slack.fail_next = ["ratelimited"]

    assert _deliver(slack, repo, clock)["retrying"] == 1
    row = repo.rows[KEY]
    assert row.delivery_state == "retrying" and row.error_code == "ratelimited"
    assert _deliver(slack, repo, clock) == {"delivered": 0, "retrying": 0, "failed_delivery": 0}

    clock.now += timedelta(minutes=5)
    assert _deliver(slack, repo, clock)["delivered"] == 1
    assert slack.posts == 1 and repo.rows[KEY].delivery_state == "delivered"


def test_an_unregistered_channel_is_a_visible_failure_not_a_success():
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    assert _deliver(slack, repo, channels=frozenset())["failed_delivery"] == 1
    row = repo.rows[KEY]
    assert row.delivery_state == "failed_delivery" and row.error_code == "unregistered_channel"
    assert row.delivered_version == 0 and slack.posts == 0
    assert notices.console_row(row)["resendable"] is True


def test_a_channel_without_the_bot_is_a_visible_failure():
    repo, slack = _repo(), FakeSlack(member=False)
    repo.record(_accepted())
    assert _deliver(slack, repo)["failed_delivery"] == 1
    assert repo.rows[KEY].error_code == "not_member"
    assert repo.resend(KEY) is True
    slack.member = True
    assert _deliver(slack, repo)["delivered"] == 1


def test_a_deleted_comment_is_replaced_and_its_ts_changes():
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    _deliver(slack, repo)
    first = repo.rows[KEY].reply_ts
    slack.thread.clear()  # 사람이 댓글을 지웠다

    repo.record(_row(notices.PROCESSING, JOB_A, T0))
    _deliver(slack, repo)

    assert slack.posts == 2
    assert repo.rows[KEY].reply_ts != first
    assert list(slack.thread) == [repo.rows[KEY].reply_ts]


def test_a_crash_after_posting_reuses_the_comment_instead_of_posting_again():
    """게시는 됐는데 `reply_ts` 를 못 적고 죽었다 — 다음 시도는 스레드에서 찾는다."""
    clock, slack = Clock(), FakeSlack()
    repo = _repo(clock)
    repo.record(_accepted())
    row = repo.pending(WS)[0]
    repo.begin_attempt(row)
    slack.chat_postMessage(
        channel=CH,
        thread_ts=TS,
        text="x",
        metadata={"event_type": notices.METADATA_EVENT, "event_payload": {"key": KEY.digest}},
    )

    _deliver(slack, repo, clock)

    assert slack.posts == 1 and len(slack.thread) == 1
    assert repo.rows[KEY].delivery_state == "delivered"
    assert slack.updates == [repo.rows[KEY].reply_ts]


def test_retries_stop_after_the_cap_and_are_visible():
    clock, slack = Clock(), FakeSlack()
    repo = _repo(clock)
    repo.record(_accepted())
    for _ in range(notices.MAX_ATTEMPTS):
        slack.fail_next = ["internal_error"]
        _deliver(slack, repo, clock)
        clock.now += timedelta(hours=2)
    assert repo.rows[KEY].delivery_state == "failed_delivery"
    assert repo.rows[KEY].error_code == "retry_exhausted"


def test_slack_exception_text_never_reaches_state():
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    slack.fail_next = ["weird error! " + LEAK]
    _deliver(slack, repo)
    row = repo.rows[KEY]
    assert LEAK not in row.error_code and row.error_code == "slack_unavailable"


def test_a_newer_phase_arriving_during_delivery_is_sent_next_round():
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    row = repo.pending(WS)[0]
    repo.record(_row(notices.PROCESSING, JOB_A, T0))  # Archiver 가 v1 을 보내는 사이
    notices.deliver_one(slack, repo, row, allowed_channels={CH})
    assert repo.rows[KEY].delivery_state == "pending"
    _deliver(slack, repo)
    assert "처리 중" in next(iter(slack.thread.values()))["text"]
    assert slack.posts == 1


# --- worker 쪽 ---------------------------------------------------------------------


def _item(state, code="", job="", revision=""):
    return ImageDescriptionBatchItem(
        workspace=WS,
        channel_id=CH,
        file_id=FILE,
        state=state,
        message_ts=TS,
        code=code,
        job_id=job,
        revision_id=revision,
    )


@pytest.mark.parametrize(
    ("state", "code", "expected"),
    [
        ("blocked", "provider_temporarily_unavailable", (notices.DEFERRED, "provider_unavailable")),
        ("blocked", "quota_exhausted", (notices.DEFERRED, "limit")),
        ("closed_without_revision", "model_timeout", (notices.FAILED, "provider_error")),
        ("closed_without_revision", "", (notices.FAILED, "unusable_output")),
        ("blocked", "access_unverified", (notices.FAILED, "blocked")),
        ("blocked", "duplicate_job", None),
    ],
)
def test_worker_outcomes_map_to_phases(state, code, expected):
    assert notices.worker_phase(state, code) == expected


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.last = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, sql, args=None):
        text = " ".join(sql.split())
        self.last = next(value for key, value in self.rows.items() if text.startswith(key))

    def fetchone(self):
        return self.last


class _Conn:
    def __init__(self, rows):
        self._cursor = _Cursor(rows)

    def cursor(self):
        return self._cursor


def _published(*, entries: int, status="described", transcript_status="transcribed"):
    return _Conn(
        {
            "SELECT status, description, tags, provider FROM archiver_image_description_revision": {
                "status": status,
                "description": "현장 사진 010-1234-5678",
                "tags": ["현장"],
                "provider": "google",
            },
            "SELECT id, status, body FROM archiver_image_transcript_revision": {
                "id": "e" * 32,
                "status": transcript_status,
                "body": "안전제일",
            },
            "SELECT count(*) AS n FROM image_search_entry": {"n": entries},
        }
    )


def test_worker_records_completion_only_from_published_guarded_values():
    repo = _repo()
    record = notices.worker_status_recorder(repo)
    record(_item("published", job=JOB_A, revision="d" * 32), connection=_published(entries=2))
    row = repo.rows[KEY]
    assert row.phase == notices.COMPLETED and row.payload["search_published"] is True
    assert "010-1234-5678" not in str(row.payload)


def test_worker_marks_uncertain_and_unconfirmed_search():
    repo = _repo()
    record = notices.worker_status_recorder(repo)
    record(
        _item("published", job=JOB_A, revision="d" * 32),
        connection=_published(entries=1, transcript_status="uncertain"),
    )
    row = repo.rows[KEY]
    assert row.phase == notices.UNCERTAIN
    assert row.payload["search_published"] is False


def test_a_failing_status_store_never_raises_into_the_worker():
    class Broken:
        def record(self, _row):
            raise RuntimeError("db down: " + LEAK)

    record = notices.worker_status_recorder(Broken())
    record(_item("blocked", code="provider_temporarily_unavailable"))  # 예외가 나오지 않는다
    record(_item("published", job=JOB_A, revision="d" * 32), connection=None)


def test_worker_status_needs_no_slack_capability():
    import inspect

    source = inspect.getsource(notices.worker_status_recorder)
    assert "chat_" not in source and "client" not in source


def test_doc_payload_carries_masked_name_and_basic_ocr_state():
    doc = SimpleNamespace(name="현장 010-1234-5678.png", conversion_state="succeeded", text="")
    payload = notices.doc_payload(doc)
    assert payload["ocr"] == "empty"
    assert "010-1234-5678" not in payload["file"]


def test_a_missing_chat_write_scope_is_a_visible_failure_not_an_endless_retry():
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    slack.fail_next = ["missing_scope"]
    assert _deliver(slack, repo)["failed_delivery"] == 1
    assert repo.rows[KEY].error_code == "missing_scope"


def test_the_archiver_manifest_can_post_status_comments():
    from pathlib import Path

    manifest = (
        Path(__file__).resolve().parents[1] / "docs" / "pilot" / "archiving-app-manifest.yaml"
    ).read_text(encoding="utf-8")
    assert "      - chat:write" in manifest.splitlines()


# --- 소급·과거 이미지는 댓글을 만들지 않는다 -------------------------------------------


def _key(file_id: str, ts: str = TS) -> notices.NoticeKey:
    return notices.NoticeKey(WS, CH, ts, file_id)


def test_two_backfilled_images_and_one_realtime_image_make_exactly_one_comment():
    repo, slack = _repo(), FakeSlack()
    old_a = _key("F11111111", "1790000000.000001")
    old_b = _key("F22222222", "1790000100.000001")
    live = _key("F33333333")
    # 과거 아카이브 이미지 둘: worker 만 처리했다(접수 없음).
    for key, job in ((old_a, JOB_A), (old_b, JOB_B)):
        repo.record(notices.NoticeRow(key=key, phase=notices.PROCESSING, job_id=job))
        repo.record(notices.NoticeRow(key=key, phase=notices.COMPLETED, job_id=job,
                                      payload=_success()))
    # 실시간 이미지 하나: Archiver 접수 → worker 완료.
    repo.record(notices.NoticeRow(key=live, phase=notices.ACCEPTED, payload=_accepted().payload))
    repo.record(notices.NoticeRow(key=live, phase=notices.PROCESSING, job_id="c" * 32))
    repo.record(notices.NoticeRow(key=live, phase=notices.COMPLETED, job_id="c" * 32,
                                  payload=_success()))

    for _ in range(3):
        _deliver(slack, repo)

    assert slack.posts == 1 and len(slack.thread) == 1
    assert {row.key for row in repo.rows.values() if row.reply_ts} == {live}
    # 과거 이미지도 DB·콘솔에는 남는다 — 「알리지 않음」 으로.
    for key in (old_a, old_b):
        shown = notices.console_row(repo.rows[key])
        assert shown["phase"] == "completed"
        assert shown["deliveryState"] == "not_announced" and shown["resendable"] is False
        assert repo.resend(key) is False


def test_pending_returns_only_rows_marked_for_slack():
    repo = _repo()
    repo.record(notices.NoticeRow(key=_key("F11111111"), phase=notices.FAILED,
                                  payload={"reason": "blocked"}))
    repo.record(_accepted())
    assert [row.key for row in repo.pending(WS)] == [KEY]


def test_worker_writes_never_touch_delivery_columns():
    """worker 의 새 판은 전달 칸을 바꾸지 않는다 — 「보낼 차례」 는 판 비교로 읽는다."""
    repo, slack = _repo(), FakeSlack()
    repo.record(_accepted())
    _deliver(slack, repo)
    before = repo._rows[KEY]
    repo.record(_row(notices.PROCESSING, JOB_A, T0))
    after = repo._rows[KEY]
    for name in ("reply_ts", "delivered_version", "delivery_state", "attempt_version",
                 "attempts", "next_attempt_at", "error_code", "slack_eligible"):
        assert getattr(after, name) == getattr(before, name), name
    assert repo.rows[KEY].delivery_state == "pending"  # 읽을 때는 새 판이 보낼 차례다
