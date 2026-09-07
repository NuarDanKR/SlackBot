"""전문가 측정 스크립트 (B-27b/B-28).

이 스크립트의 값은 **하나**다 — 답변에 근거에 없는 값이 들어갔는지 잡는 것.
"어느 답이 더 좋은가" 는 기계가 판정할 수 없고, 판정한 척하면 그 숫자를 보고
잘못된 결정을 하게 된다.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_specialist.py"


@pytest.fixture(scope="module")
def mod():
    """스크립트를 모듈로 불러온다.

    **`sys.modules` 에 먼저 등록해야 한다.** 스크립트가
    `from __future__ import annotations` 를 쓰므로 dataclass 가 문자열 주석을 풀 때
    자기 모듈을 찾는데, 등록 전이면 `None` 이라 AttributeError 로 죽는다.
    """
    spec = importlib.util.spec_from_file_location("measure_specialist", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 근거에 없는 값 ---------------------------------------------------------
def test_a_value_absent_from_the_evidence_is_flagged(mod):
    """지어낸 금액이 답변에 들어가고 거기에 우리 출처가 붙는 것이 가장 위험하다."""
    evidence = "> [2026-08-20 09:00] 홍길동: 기성금 3억 청구했습니다"

    assert mod.unsupported_values("기성금은 5억입니다.", evidence) == ["5억"]
    assert mod.unsupported_values("기성금은 3억입니다.", evidence) == []


def test_commas_do_not_create_a_false_alarm(mod):
    """모델이 `1,000억` 을 `1000억` 으로 적는다. 그건 지어낸 값이 아니다."""
    evidence = "PF대출 1,000억 기표"

    assert mod.unsupported_values("PF대출은 1000억입니다.", evidence) == []


def test_a_converted_value_is_caught(mod):
    """환산은 원문이 아니다. 같은 금액이어도 근거에 없는 표기다."""
    evidence = "기성금 3억 2천만원 청구"

    assert mod.unsupported_values("기성금은 320,000,000원입니다.", evidence)


def test_a_truncated_value_is_a_known_blind_spot(mod):
    """**이 검사가 못 잡는 것.**

    「3억 2천만원」 을 「약 3억」 으로 줄여 적으면 잡히지 않는다 — `3억` 이 근거에
    실제로 있기 때문이다. 값의 존재만 보고 **문맥은 보지 않는다.**

    이걸 잡으려면 「이 값이 저 값의 일부인가」 를 판정해야 하고, 그건 기계가 틀리기
    쉬운 자리다. 잘못 잡으면 멀쩡한 답이 「지어냈다」 로 보고되고, 그 오탐이 잦으면
    사람이 이 보고서를 안 본다. 못 잡는 쪽을 고른 것이고, 그 선택을 여기 적어 둔다.
    """
    evidence = "기성금 3억 2천만원 청구"

    assert mod.unsupported_values("기성금은 약 3억입니다.", evidence) == []


def test_single_digits_are_not_treated_as_values(mod):
    """한 자리 숫자는 문장에 흔해서 잡으면 잡음만 는다."""
    evidence = "회의를 열었습니다"

    assert mod.unsupported_values("3가지 안건이 있었습니다.", evidence) == []


def test_dates_are_values_too(mod):
    """날짜를 지어내는 것은 금액을 지어내는 것과 같다."""
    evidence = "> [2026-08-20 09:00] 홍길동: 착공했습니다"

    assert mod.unsupported_values("9월 30일에 착공했습니다.", evidence)


# --- 권한을 넓히지 않는다 ---------------------------------------------------
def test_the_replay_scope_comes_from_the_citations(mod):
    """`scope` 는 요약이라 권한을 복원할 수 없다.

    인용된 채널로만 좁힌다 — 원래 범위의 부분집합이라 넓어지는 일이 없다.
    """
    channels = mod.channels_from([
        "#팀_전산(ABB155)_주간보고, 📄t.md(2026-08-20)",
        "[mgmt] #현장_김해외동(180182)_채팅방, 📄x.md(2026-08-01)",
    ])

    assert channels == [
        "#팀_전산(ABB155)_주간보고",
        "#현장_김해외동(180182)_채팅방",
    ]


def test_a_question_without_citations_is_skipped(mod, monkeypatch):
    """인용이 없으면 재생 범위를 만들 수 없다. **짐작하지 않는다** —

    넓게 잡으면 그 사람이 볼 수 없던 자료로 답을 만든다.
    """
    from tybot.console import reader

    monkeypatch.setattr(
        reader,
        "_read_qa_records",
        lambda days: [
            {"question": "인용 없음", "reason": "answered", "hits": 3, "citations": []},
            {"question": "인용 있음", "reason": "answered", "hits": 1,
             "citations": ["#팀_전산(ABB155)_주간보고, 📄t.md(2026-08-20)"]},
        ],
    )

    picked = [r["question"] for r in mod.load_questions(14, 10)]

    assert picked == ["인용 있음"]


def test_zero_hit_questions_are_skipped(mod, monkeypatch):
    """근거 0건이면 전문가에게도 묻지 않으므로 비교할 것이 없다."""
    from tybot.console import reader

    monkeypatch.setattr(
        reader,
        "_read_qa_records",
        lambda days: [
            {"question": "근거 없음", "reason": "no_hits", "hits": 0,
             "citations": ["#팀_전산(ABB155)_주간보고, 📄t.md(2026-08-20)"]},
        ],
    )

    assert mod.load_questions(14, 10) == []


# --- 운영 통계를 오염시키지 않는다 ------------------------------------------
def test_the_probe_never_records_to_the_call_table(mod):
    """재생한 질문이 `specialist_call` 에 쌓이면 「전문가가 실제로 몇 번 답했나」 가

    부풀고, 그 표를 믿을 수 없게 된다.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    assert "record_call_row=False" in source
    assert "from tybot.slack.pilot import specialist_hook" not in source, (
        "운영 훅은 기록을 남긴다 — 측정에 쓰면 안 된다"
    )


def test_it_refuses_to_spend_money_without_consent(mod):
    """질문 하나에 모델을 최소 세 번 부른다. 실제 비용이 든다."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--yes"' in source
    assert "if not args.yes:" in source


def test_the_output_file_carries_no_answer_text(mod):
    """업무 내용이 파일로 새지 않게 한다. 담는 것은 건수·값·시간이다."""
    source = SCRIPT.read_text(encoding="utf-8")
    payload = source[source.index('payload = {'):source.index('Path(args.out)')]

    for leaked in ('"masterText"', '"specialistText"', "r.master_text", "r.special_text"):
        assert leaked not in payload, f"결과 파일에 답변 본문이 담긴다: {leaked}"
