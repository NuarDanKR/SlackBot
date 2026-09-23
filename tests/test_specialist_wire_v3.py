"""전문 봇 호출 계약 v3 — 검색 도구를 주는 호출의 형식.

설계: `docs/design/specialist-v3-tool-contract.md`

## 이 시험이 무엇을 고정하나
PF 개발자가 맞출 대상이 우리 구현이면, 우리가 코드를 고칠 때마다 그쪽이 깨진다.
**고정된 스키마가 기준**이어야 양쪽이 각자 시험한다. 그래서 여기서 고정하는 것은
동작이 아니라 **계약**이다 — 필드 이름, 상태 값, 거절 조건.

## v2 와 갈리는 지점
v2 는 「근거 없는 요청」 을 요청 단계에서 막았다. v3 는 도구로만 자료가 나가므로
씨앗 근거를 요구하지 않고, 대신 **응답에 근거 ID 가 없으면 버린다.** 규칙이 사라진
것이 아니라 판정 시점이 옮겼다 — 이 파일이 그 이동을 고정한다.
"""
from __future__ import annotations

import json

import pytest

from tybot import specialist_wire_v3 as v3
from tybot.specialist_wire import WireViolation


def _request(**over) -> v3.Request:
    values = {
        "workspace": "tyit",
        "authorization_id": "auth-2026-09-23",
        "question": "지난주 정산 금액",
        "tool_token": v3.new_tool_token(),
    }
    values.update(over)
    return v3.Request(**values)


def _body(request: v3.Request) -> dict:
    return json.loads(request.body())


# --- 요청 ---------------------------------------------------------------------
def test_a_request_needs_no_seed_evidence():
    """v2 는 여기서 막았다. v3 는 도구로만 자료가 나가므로 씨앗이 없어도 된다."""
    body = _body(_request())
    assert body["evidence"] == []
    assert body["schema"] == v3.SCHEMA_REQUEST


def test_a_request_without_a_tool_token_is_refused():
    """토큰 없이 보내면 도구를 못 쓰는데 v3 는 씨앗 근거도 요구하지 않는다.

    그러면 **근거 없이 답하라는 요청**이 된다 — 가장 나쁜 조합이다.
    """
    with pytest.raises(WireViolation, match="도구 토큰"):
        _request(tool_token="")


def test_a_request_must_allow_at_least_one_tool():
    with pytest.raises(WireViolation, match="v2"):
        _request(allow=())


def test_an_unknown_tool_cannot_be_allowed():
    """`ToolBox` 가 아는 이름과 달라지면 「알 수 없는 도구」 가 조용히 돌아오고,
    모델은 그 도구가 없는 줄 알고 다른 길로 간다."""
    with pytest.raises(WireViolation, match="모르는 도구"):
        _request(allow=("search", "read_everything"))


def test_the_tool_block_carries_the_budget_so_hermes_can_count_too():
    body = _body(_request())
    budget = body["tools"]["budget"]
    assert budget["max_calls"] > 0
    assert budget["max_seconds"] > 0
    # 판정은 우리가 한다. 보낸 숫자를 그쪽이 지킬 것이라고 믿지 않는다.
    assert body["tools"]["endpoint"] == v3.TOOL_ENDPOINT


def test_the_deadline_is_long_enough_for_several_searches():
    """v2 의 15초로는 첫 검색도 못 끝낸다."""
    assert v3.DEADLINE_MS >= 60_000


def test_a_follow_up_carries_coordinates_not_prose():
    """이어 가는 것은 이전 답변 문장이 아니라 그 답변이 읽은 원문 좌표다(원칙 1)."""
    body = _body(_request(follow_up=(v3.FollowUpRef(id="e0", locator="opaque-1"),)))

    (ref,) = body["follow_up"]
    assert set(ref) == {"id", "locator"}


def test_every_request_gets_its_own_tool_token():
    assert _request().tool_token != _request().tool_token


def test_the_request_scope_still_comes_from_one_authorization():
    with pytest.raises(WireViolation):
        _request(authorization_id="  ")


# --- 도구 호출 -----------------------------------------------------------------
def _call(**over) -> bytes:
    payload = {
        "schema": v3.SCHEMA_TOOL_CALL,
        "request_id": "req-1",
        "token": "tok-1",
        "call_id": "t1",
        "tool": "search",
        "args": {"query": "정산"},
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_a_tool_call_parses_into_its_parts():
    got = v3.parse_tool_call(_call())
    assert got.tool == "search"
    assert got.args == {"query": "정산"}
    assert got.call_id == "t1"


def test_parsing_looks_at_shape_only_not_at_the_token():
    """형식과 권한을 한 함수에서 보면 형식 오류와 권한 거절이 같은 예외로 나온다.

    그러면 「계약이 틀렸나 권한이 없나」 를 로그에서 못 가린다.
    """
    # 아무 토큰이나 형식만 맞으면 파싱은 통과한다. 확인은 호출부 몫이다.
    assert v3.parse_tool_call(_call(token="무엇이든")).token == "무엇이든"


@pytest.mark.parametrize("missing", ["request_id", "token", "call_id"])
def test_a_tool_call_without_its_identifiers_is_refused(missing):
    with pytest.raises(WireViolation, match=missing):
        v3.parse_tool_call(_call(**{missing: ""}))


def test_a_tool_call_for_an_unknown_tool_is_refused():
    with pytest.raises(WireViolation, match="모르는 도구"):
        v3.parse_tool_call(_call(tool="read_everything"))


def test_an_oversized_tool_call_is_refused():
    with pytest.raises(WireViolation, match="상한"):
        v3.parse_tool_call(_call(args={"query": "가" * 40_000}))


# --- 도구 결과 -----------------------------------------------------------------
def test_a_failed_tool_still_answers_ok_with_words():
    """모델이 도구 실패를 **모른 채** 답을 만드는 것이 가장 나쁘다.

    HTTP 오류로 끊으면 Hermes 쪽 예외 처리에 맡기게 되고, 그쪽이 조용히 삼키면
    우리는 알 방법이 없다.
    """
    payload = json.loads(v3.tool_result("t1", "(도구 오류: search)"))

    assert payload["ok"] is True
    assert "도구 오류" in payload["text"]


def test_an_exhausted_budget_is_a_result_not_an_error():
    payload = json.loads(
        v3.tool_result("t1", "(검색 예산을 다 썼습니다…)", budget_left={"calls_left": 0})
    )
    assert payload["ok"] is True
    assert payload["budget"]["calls_left"] == 0


def test_a_contract_violation_is_the_one_thing_that_is_not_ok():
    """재시도하면 같은 거절이 반복되며 예산만 태운다. 도구 실패와 갈라야 한다."""
    payload = json.loads(v3.tool_refusal("t1", "토큰이 만료됐습니다"))

    assert payload["ok"] is False
    assert payload["reason"]
    assert payload["text"] == ""


def test_a_tool_result_carries_opaque_ids_only():
    """채널 ID·파일 경로·message_ts 를 주면 모아서 우리 구조를 재구성할 수 있다."""
    payload = json.loads(
        v3.tool_result("t1", "…", evidence=[{"id": "e7", "kind": "channel_line"}])
    )
    (item,) = payload["evidence"]
    assert set(item) == {"id", "kind"}


# --- 응답 ---------------------------------------------------------------------
def _response(**over) -> bytes:
    payload = {
        "schema": v3.SCHEMA_RESPONSE,
        "request_id": "req-1",
        "status": v3.STATUS_ANSWERED,
        "answer": "정산 금액은 …",
        "used_evidence": ["e7"],
        "uncertain": [],
        "usage": {"tool_calls": 3},
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _validate(raw: bytes, *, issued=frozenset({"e7", "e8"})) -> v3.Answer:
    return v3.validate_response(raw, request_id="req-1", issued_evidence=issued)


def test_a_good_answer_passes():
    got = _validate(_response())
    assert got.status == v3.STATUS_ANSWERED
    assert got.used_evidence == ("e7",)
    assert not got.should_fall_back


def test_an_invented_evidence_id_kills_the_answer():
    """지어냈거나 다른 요청의 것이다. 어느 쪽이든 출처를 보증할 수 없고,
    보증 못 하는 답에는 출처를 붙일 수 없다(원칙 2)."""
    with pytest.raises(WireViolation, match="발급하지 않은"):
        _validate(_response(used_evidence=["e7", "e99"]))


def test_answering_with_no_evidence_at_all_is_refused():
    """v2 는 요청에서 막았고 v3 는 여기서 막는다. 판정 시점이 옮겼을 뿐이다."""
    with pytest.raises(WireViolation, match="근거 없이"):
        _validate(_response(used_evidence=[]))


def test_a_response_for_another_request_is_refused():
    """소켓을 공유하거나 재시도가 엇갈렸을 때 난다."""
    with pytest.raises(WireViolation, match="request_id"):
        _validate(_response(request_id="req-9"))


def test_the_four_statuses_are_told_apart():
    """「모른다」 와 「못 찾았다」 와 「고장났다」 를 하나로 묶으면 원인을 못 가린다."""
    assert set(v3.STATUSES) == {"answered", "no_evidence", "refused", "failed"}


def test_only_a_failure_falls_back_to_the_master():
    """`refused` 는 권한·계약 때문에 답하지 않은 것이다. 마스터가 대신 답하면
    그 판단이 무의미해진다."""
    assert _validate(_response(status="failed", used_evidence=[], answer="")).should_fall_back
    assert not _validate(
        _response(status="refused", used_evidence=[], answer="권한이 없습니다")
    ).should_fall_back


def test_no_evidence_is_an_answer_not_a_failure():
    """찾아봤는데 없는 것과 고장난 것은 사람이 할 일이 다르다."""
    got = _validate(_response(status="no_evidence", used_evidence=[], answer="찾지 못했습니다"))
    assert not got.should_fall_back


def test_an_unknown_status_is_refused():
    with pytest.raises(WireViolation, match="모르는 상태"):
        _validate(_response(status="maybe"))


def test_an_overlong_answer_is_refused():
    with pytest.raises(WireViolation, match="답변이 상한"):
        _validate(_response(answer="가" * (v3.MAX_OUTPUT_CHARS + 1)))


def test_an_answered_status_with_an_empty_answer_is_refused():
    with pytest.raises(WireViolation, match="답변이 비었"):
        _validate(_response(answer="   "))


def test_usage_is_carried_for_reporting_not_for_limiting():
    """모델 비용의 주인은 프금팀이다(오너 계획 §7.1). 우리는 보기만 한다."""
    got = _validate(_response(usage={"cost_usd": 0.04, "tool_calls": 5}))
    assert got.usage["cost_usd"] == 0.04


# --- v2 와 같은 것은 같게 --------------------------------------------------------
def test_v3_reuses_the_v2_signing_rules():
    """두 벌로 두면 한쪽만 고쳐지는 날이 온다."""
    from tybot import specialist_wire

    assert v3.WireViolation is specialist_wire.WireViolation


def test_v3_does_not_open_a_new_port():
    """같은 호스트 Unix socket 만 쓴다(분리 결정 §5)."""
    source = (
        __import__("pathlib").Path(v3.__file__).read_text(encoding="utf-8")
    )
    for banned in ("http://", "https://", "0.0.0.0", "bind("):
        assert banned not in source, f"v3 가 네트워크를 엽니다: {banned}"
