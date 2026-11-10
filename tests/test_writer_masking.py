"""수집 writer 는 **가린 뒤에 쓴다** (B-86).

오너 결정으로 「민감정보가 있으면 그 줄을 버린다」 가 「가리고 보관한다」 로
바뀌었다. 완화가 아니다 — 평문이 **파일·결과 객체·로그 어디에도** 남지 않는다는
조건이 먼저다.

이 파일이 보는 것은 그 조건이다. 가려서 보관하는 기능 자체는
`test_sensitive_mask.py` 가 보고, 여기서는 **평문이 새는 길**을 하나씩 막는다.

새는 길은 조용하다. 파일에는 가린 글이 들어갔는데 예외 메시지에 원문이 실려
서버 로그로 나가면, 아카이브만 보는 사람은 영원히 모른다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from tybot.archive import writer

RRN = "900101-1234567"
BARE_RRN = "9001011234567"
SECRETS = ("900101", "1234567", "홍길동", "hong@ty.com", "010-1234-5678")


def _msg(text: str, *, minute: int = 15, speaker: str = "김과장", is_bot: bool = False):
    return writer.IncomingMessage(
        ts=datetime(2026, 8, 12, 9, minute, tzinfo=UTC),
        speaker=speaker,
        text=text,
        is_bot=is_bot,
    )


def _ingest(tmp_path, *messages, **kw):
    args = {
        "workspace": "pilot",
        "channel": "#팀_자금(ABB540)_주간보고",
        "acl": ["#팀_자금(ABB540)_주간보고"],
    }
    args.update(kw)
    return writer.ingest(tmp_path, messages=list(messages), **args)


# --- 1. 파일에 평문이 없다 -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code", "token"),
    [
        (f"주민등록번호 {RRN} 입니다", "rrn", "[주민등록번호1]"),
        (f"번호 {BARE_RRN}", "rrn", "[주민등록번호1]"),
        ("연락처 010-1234-5678 입니다", "phone", "[전화번호1]"),
        ("메일 hong@ty.com 로 주세요", "email", "[이메일1]"),
        ("계약자: 홍길동", "name", "[이름1]"),
    ],
)
def test_the_written_line_holds_the_mask_and_not_the_value(tmp_path, raw, code, token):
    got = _ingest(tmp_path, _msg(raw))

    text = got.path.read_text(encoding="utf-8")
    assert token in text
    assert got.mask_counts == {code: 1}
    assert got.mask_revision == writer.mask_sensitive("").revision


def test_no_fragment_of_the_original_survives_anywhere_in_the_file(tmp_path):
    got = _ingest(
        tmp_path,
        _msg(f"성명: 홍길동 / {RRN} / 010-1234-5678 / hong@ty.com"),
    )

    text = got.path.read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in text, f"평문이 파일에 남았다: {secret}"


def test_the_surrounding_sentence_is_kept(tmp_path):
    """가리는 것이 버리는 것으로 되돌아가면 아무것도 나아지지 않는다."""
    got = _ingest(tmp_path, _msg(f"기성금 3억 청구 건, 담당 {RRN} 확인 바랍니다"))

    text = got.path.read_text(encoding="utf-8")
    assert got.written == 1
    assert "기성금 3억 청구" in text
    assert "확인 바랍니다" in text


# --- 2. 결과 객체에 평문이 없다 -------------------------------------------------


def test_the_result_object_carries_codes_and_counts_only(tmp_path):
    got = _ingest(tmp_path, _msg(f"성명: 홍길동 / {RRN}"))

    blob = repr(got)
    for secret in SECRETS:
        assert secret not in blob, f"결과 객체에 평문이 남았다: {secret}"
    assert got.mask_counts == {"name": 1, "rrn": 1}


def test_clean_text_reports_no_masking(tmp_path):
    """가릴 것이 없었는데 `masked` 가 서면 감사기록이 거짓말을 한다."""
    got = _ingest(tmp_path, _msg("기성금 3억 청구"))

    assert got.mask_counts == {}
    assert got.mask_revision == ""


# --- 3. 로그에 평문이 없다 -----------------------------------------------------


def test_nothing_in_the_log_carries_the_original(tmp_path, caplog):
    """파일은 가렸는데 로그로 새면 아카이브만 보는 사람은 영원히 모른다."""
    with caplog.at_level(logging.DEBUG):
        _ingest(tmp_path, _msg(f"성명: 홍길동 / {RRN} / hong@ty.com"))

    blob = caplog.text
    for secret in SECRETS:
        assert secret not in blob, f"로그에 평문이 남았다: {secret}"


def test_a_refusal_reason_names_the_shape_not_the_value(tmp_path, monkeypatch):
    """가릴 수 없는 모양은 예전처럼 제외한다 — 사유에 값을 싣지 않고.

    마스커를 못 쓰는 상황(정책 미적용·회귀)에서도 평문이 보관되지 않는 **마지막
    관문**이 살아 있어야 한다.
    """
    monkeypatch.setattr(writer, "mask_sensitive", _Unmasked)
    got = _ingest(tmp_path, _msg(f"주민등록번호 {RRN}"))

    assert got.written == 0
    assert len(got.refused) == 1
    speaker, reason = got.refused[0]
    assert speaker == "김과장"
    for secret in SECRETS:
        assert secret not in reason


class _Unmasked:
    """아무것도 가리지 않는 마스커. 마지막 관문이 사는지 보려고 쓴다."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.counts: dict[str, int] = {}
        self.revision = "none"
        self.masked = False


# --- 4. 마스킹이 쓰기보다 먼저다 -----------------------------------------------


def test_masking_happens_before_the_line_is_formatted(tmp_path, monkeypatch):
    """`format_line` 이 평문을 한 번이라도 손에 쥐면 안 된다.

    그 함수가 만드는 문자열은 dedupe 키·예외 메시지·임시 파일로 흘러간다.
    뒤에 가리면 파일만 깨끗하고 나머지 길이 전부 열린다.
    """
    seen: list[str] = []
    original = writer.format_line
    monkeypatch.setattr(
        writer, "format_line",
        lambda msg: seen.append(msg.text) or original(msg),
    )

    _ingest(tmp_path, _msg(f"성명: 홍길동 / {RRN}"))

    assert seen, "format_line 이 불리지 않았다 — 시험이 경로를 못 탔다"
    for text in seen:
        for secret in SECRETS:
            assert secret not in text, "format_line 이 평문을 받았다"


def test_the_dedupe_key_is_built_from_the_masked_line(tmp_path):
    """같은 내용을 다시 넣어도 두 벌이 되지 않는다."""
    first = _ingest(tmp_path, _msg(f"성명: 홍길동 / {RRN}"))
    second = _ingest(tmp_path, _msg(f"성명: 홍길동 / {RRN}"))

    assert first.written == 1
    assert second.written == 0
    assert second.path.read_text(encoding="utf-8").count("[이름1]") == 1


# --- 5. 봇 발언은 여전히 들어오지 않는다 ---------------------------------------


def test_masking_does_not_open_a_door_for_bot_output(tmp_path):
    """원칙 1 은 그대로다. 가린다고 봇 발언이 근거가 되지는 않는다."""
    got = _ingest(
        tmp_path,
        _msg("사람 발언"),
        _msg(f"요약: {RRN}", speaker="태봇", is_bot=True, minute=16),
    )

    text = got.path.read_text(encoding="utf-8")
    assert got.skipped_bot == 1
    assert "태봇" not in text
    assert got.mask_counts == {}, "버릴 줄을 먼저 가려 횟수가 부풀었다"
