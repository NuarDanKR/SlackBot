"""전달 직전 마스킹 (B-86).

수집에서 가렸는데 전달에서 또 보는 이유는 둘이다.

1. **모델이 평문을 돌려줄 수 있다.** 근거에 없던 번호를 지어내든, 마스킹
   이전에 쌓인 옛 원문을 읽었든, 그 문장이 그대로 Slack 으로 나간다
2. 마스킹은 2026-11-10 부터다. 그 전 원문은 평문이고, **아카이브는 append-only
   라 소급해 고치지 않는다** — 읽는 쪽에서 가리는 수밖에 없다

두 번째가 「근거 보기」 를 중요하게 만든다. 답변 본문만 가리면 버튼을 눌렀을 때
평문이 나온다. 가장 눈에 안 띄는 구멍이다.
"""

from __future__ import annotations

import pytest

from tybot import evidence_view
from tybot.answer import Answer

RRN = "900101-1234567"
PHONE = "010-1234-5678"


def _answer(text: str, citations=()) -> Answer:
    return Answer(text, list(citations), "m", 0.0, 1, "answered")


# --- 1. 답변 본문 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "token"),
    [
        (f"담당자 주민등록번호는 {RRN} 입니다", "[주민등록번호1]"),
        (f"연락처는 {PHONE} 입니다", "[전화번호1]"),
        ("메일은 hong@ty.com 입니다", "[이메일1]"),
        ("계약자: 홍길동", "[이름1]"),
    ],
)
def test_a_model_answer_in_the_clear_is_masked_before_delivery(raw, token):
    got = _answer(raw).to_slack()

    assert token in got
    for secret in ("900101", "1234567", "1234-5678", "hong@ty.com", "홍길동"):
        assert secret not in got


def test_the_same_funnel_feeds_message_canvas_and_the_audit_record():
    """`to_slack()` 이 셋이 공유하는 **하나의 출구**다.

    여기서 막으면 세 곳이 같이 막히고, 한 곳만 고치는 날이 오지 않는다.
    """
    plain = _answer(f"연락처 {PHONE}")

    assert PHONE not in plain.to_slack()
    assert PHONE not in plain.to_slack(preserve_markdown=True)


def test_business_numbers_in_an_answer_are_not_touched():
    """가리는 것이 답을 지우는 것으로 되돌아가면 아무것도 나아지지 않는다."""
    got = _answer("기성금 3억, 집행률 72%, 문서번호 2026-000123").to_slack()

    assert "3억" in got
    assert "72%" in got
    assert "2026-000123" in got


def test_citations_are_left_alone():
    """출처는 시스템이 만든 좌표다. 가리면 사람이 확인하러 갈 자리를 잃는다."""
    got = _answer(
        "확인했습니다",
        citations=["#팀_자금(ABB540)_주간보고, 📄2026-11.md(2026-11-05)"],
    ).to_slack()

    # 표시 형식은 `_display_citation` 이 접는다(채널 + 날짜). 여기서 보는 것은
    # **마스커가 그 줄을 건드리지 않았다**는 것뿐이다.
    assert "#팀_자금(ABB540)_주간보고" in got
    assert "2026-11-05" in got


def test_masking_an_already_masked_answer_changes_nothing():
    """수집에서 한 번, 전달에서 또 한 번 — 두 번 가려도 같아야 한다."""
    once = _answer(f"연락처 {PHONE}").to_slack()
    twice = _answer(once).to_slack()

    assert PHONE not in twice
    assert twice.count("[전화번호1]") == 1


# --- 2. 「근거 보기」 ----------------------------------------------------------


def _line(text: str) -> evidence_view.EvidenceLine:
    return evidence_view.EvidenceLine(
        channel="#팀_자금(ABB540)_주간보고",
        ts="2026-08-12 09:15",
        speaker="김과장",
        text=text,
        workspace="pilot",
    )


def test_an_old_unmasked_archive_line_is_masked_when_shown(monkeypatch):
    """마스킹 이전에 쌓인 줄에는 평문이 그대로 있다.

    아카이브는 append-only 라 소급해 고치지 않는다 — 읽는 쪽에서 가린다.
    """
    monkeypatch.setattr(evidence_view, "_stamp", lambda ts: ts)
    got = evidence_view.stored_report([_line(f"담당 {RRN} 확인")], dropped=[])

    assert "[주민등록번호1]" in got
    assert "900101" not in got and "1234567" not in got


def test_the_rest_of_the_evidence_line_survives(monkeypatch):
    monkeypatch.setattr(evidence_view, "_stamp", lambda ts: ts)
    got = evidence_view.stored_report(
        [_line(f"기성금 3억 청구, 담당 {RRN} 확인 바랍니다")], dropped=[]
    )

    assert "기성금 3억 청구" in got
    assert "확인 바랍니다" in got
    assert "김과장" in got


def test_two_people_in_the_evidence_stay_two_people(monkeypatch):
    monkeypatch.setattr(evidence_view, "_stamp", lambda ts: ts)
    got = evidence_view.stored_report(
        [_line("계약자: 홍길동"), _line("담당자: 김철수")], dropped=[]
    )

    assert "[이름1]" in got


# --- 3. 지어낸 값도 막는다 ------------------------------------------------------


def test_a_value_the_model_invented_is_masked_too():
    """근거에 없던 번호라도 나가면 안 된다.

    「근거에 있는 것만 말한다」 는 프롬프트 계약이고, 이 관문은 그 계약이
    깨졌을 때를 위한 것이다.
    """
    got = _answer(f"추정 연락처는 {PHONE} 으로 보입니다").to_slack()

    assert PHONE not in got
    assert "[전화번호1]" in got


# --- 4. 근거 한 줄도 가린다 (보안 검토 2026-11-10) ----------------------------


def test_the_search_terms_in_the_evidence_note_are_masked():
    """검색어는 **질문에서 왔다.**

    「홍길동 연락처 알려줘」 라고 물으면 그 말이 그대로 근거 줄에 실린다.
    본문만 가리면 가린 답변 바로 아래 줄에 평문이 남는다.
    """
    got = Answer(
        "확인했습니다", ["#팀_자금(ABB540)_주간보고, 📄2026-11.md"], "m", 0.0, 1,
        "answered", terms=["계약자: 홍길동", PHONE],
    ).to_slack()

    assert "홍길동" not in got
    assert PHONE not in got
    assert "[이름1]" in got or "[전화번호1]" in got


def test_a_withheld_attachment_name_is_masked():
    """못 읽은 첨부의 **파일명**이 근거 줄에 실린다.

    `홍길동_주민등록증.pdf` 같은 이름이 그대로 나가면, 읽지 못한 파일의 내용을
    가린 것이 무의미해진다.
    """
    got = Answer(
        "확인했습니다", ["#팀, 📄a.md"], "m", 0.0, 1, "answered",
        withheld=[f"계약자: 홍길동_{RRN}.pdf"],
    ).to_slack()

    assert "홍길동" not in got
    assert "900101" not in got


def test_the_note_still_says_what_it_needs_to_say():
    """가리는 것이 설명을 지우는 것으로 되돌아가면 안 된다."""
    got = Answer(
        "확인했습니다",
        ["#팀_자금(ABB540)_주간보고, 📄a.md", "#팀_자금(ABB540)_주간보고, 📄b.md"],
        "claude-sonnet-5", 0.0, 3, "answered",
        terms=["기성금"],
    ).to_slack()

    assert "기성금" in got
    # `doc_count` 는 출처 건수에서 나오는 파생값이다.
    assert "문서 2건" in got
    assert "원문 3줄 사용" in got
    assert "claude-sonnet-5" in got
