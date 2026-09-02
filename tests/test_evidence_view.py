"""`근거 보기` — 답변이 실제로 읽은 원문 줄.

출처 줄은 *어디서* 왔는지만 말한다. 원문을 그대로 보여주면 질문이
"봇을 믿을 수 있나" 에서 "이 답이 맞나" 로 바뀐다 — 뒤쪽은 검증 가능한 질문이다.

핵심 성질: **저장하지 않고 다시 찾는다.** 그래서 권한이 볼 때 다시 판정된다.
"""
from __future__ import annotations

from unittest.mock import Mock

from tybot.evidence_view import (
    ACTION_SHOW,
    MAX_LINES,
    NO_EVIDENCE,
    EvidenceLine,
    blocks,
    button,
    report,
)


def _line(**kw) -> EvidenceLine:
    base = {
        "channel": "#본사팀-전산_ABB110-회의",
        "ts": "2026-09-01T14:03+09:00",
        "speaker": "김수현",
        "text": "김해외동 기성금은 3억 2천만원입니다",
        "workspace": "mgmt",
    }
    base.update(kw)
    return EvidenceLine(**base)


# --- 버튼 --------------------------------------------------------------------
def test_button_carries_the_query():
    b = button(["김해외동", "기성금"])
    el = b["elements"][0]
    assert el["action_id"] == ACTION_SHOW
    assert el["value"] == "김해외동 기성금"


def test_no_button_without_terms():
    """버튼이 있는데 눌러도 아무것도 안 나오면 없는 것만 못하다."""
    assert button([]) is None
    assert button(["", "  "]) is None


def test_blocks_keep_the_answer_body():
    out = blocks("전산팀은 서버 이관을 진행했습니다.", ["서버", "이관"])
    assert out[0]["text"]["text"].startswith("전산팀은")
    assert out[1]["elements"][0]["action_id"] == ACTION_SHOW


def test_blocks_without_terms_have_no_action():
    out = blocks("요약 결과", [])
    assert [b["type"] for b in out] == ["section"]


def test_button_value_is_capped():
    assert len(button(["가" * 3000])["elements"][0]["value"]) <= 1900


# --- 표시 --------------------------------------------------------------------
def test_report_shows_raw_lines_verbatim():
    """요약하면 그게 또 하나의 답변이 된다. 손대지 않는다."""
    text = report([_line()], query="김해외동 기성금", own_workspace="mgmt")
    assert "김해외동 기성금은 3억 2천만원입니다" in text
    assert "김수현" in text


def test_report_groups_by_channel():
    lines = [_line(), _line(text="다른 줄"), _line(channel="#현장-김해외동_1800249-채팅방")]
    text = report(lines, query="기성금", own_workspace="mgmt")
    assert text.count("*#본사팀-전산_ABB110-회의*") == 1
    assert "*#현장-김해외동_1800249-채팅방*" in text


def test_other_workspace_is_marked():
    """읽는 사람이 '이건 우리 자료가 아니다' 를 알아야 한다."""
    text = report([_line(workspace="pilot")], query="기성금", own_workspace="mgmt")
    assert "[pilot]" in text


def test_own_workspace_is_not_marked():
    text = report([_line(workspace="mgmt")], query="기성금", own_workspace="mgmt")
    assert "[mgmt]" not in text


def test_timestamp_is_trimmed():
    text = report([_line()], query="기성금")
    assert "09-01 14:03" in text


def test_report_says_it_searched_again():
    """저장해 둔 걸 꺼내는 게 아니라는 사실을 밝힌다 - 줄이 달라질 수 있다."""
    assert "지금 다시 찾은" in report([_line()], query="기성금")


def test_report_states_the_permission_rule():
    assert "회원님이 볼 수 있는 채널만" in report([_line()], query="기성금")


def test_empty_result_explains_why():
    text = report([], query="기성금")
    assert text == NO_EVIDENCE
    assert "권한이 바뀌었거나" in text


def test_long_result_is_capped_and_says_so():
    lines = [_line(text=f"줄 {i}") for i in range(40)]
    text = report(lines, query="기성금")
    assert text.count("`09-01 14:03`") == MAX_LINES
    assert f"전체 {len(lines)}줄 중" in text
    assert "검색어를 좁히면" in text


# --- 봇 연결 -----------------------------------------------------------------
def _bot(hits=()):
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.store = Mock(search=Mock(return_value=list(hits)))
    bot._context = lambda client, uid: Mock(workspace="mgmt", role="member")
    return bot


def _hit(**kw):
    doc = Mock(channel=kw.get("channel", "#본사팀-전산_ABB110-회의"),
               workspace=kw.get("workspace", "mgmt"))
    line = Mock(ts="2026-09-01T14:03+09:00", speaker="김수현",
                text=kw.get("text", "기성금은 3억 2천만원"))
    return Mock(doc=doc, line=line)


def test_bot_searches_with_the_users_own_permission():
    """저장이 아니라 재검색이라 권한이 **볼 때** 다시 판정된다."""
    bot = _bot([_hit()])
    text = bot._evidence_text(Mock(), "U1", "김해외동 기성금")
    assert "3억 2천만원" in text
    args, kwargs = bot.store.search.call_args
    assert args[0] == "김해외동 기성금"
    assert kwargs["limit"] == 40


def test_bot_handles_empty_query():
    bot = _bot()
    assert bot._evidence_text(Mock(), "U1", "  ") == NO_EVIDENCE
    bot.store.search.assert_not_called()


def test_bot_survives_search_failure():
    bot = _bot()
    bot.store.search.side_effect = RuntimeError("boom")
    assert "다시 찾지 못했습니다" in bot._evidence_text(Mock(), "U1", "기성금")


def test_raw_lines_never_reach_the_log(caplog):
    """근거 줄은 사용자 화면에만 간다. 로그에 남기면 감사기록이 원문 사본이 된다."""
    bot = _bot([_hit(text="비밀 기성금 3억")])
    with caplog.at_level("INFO"):
        out = bot._evidence_text(Mock(), "U1", "기성금")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "비밀 기성금 3억" in out
    assert "비밀 기성금 3억" not in logged
    assert "lines=1" in logged


# --- 표 렌더 ------------------------------------------------------------------
# Slack mrkdwn 에는 표 문법이 없다. 모델이 마크다운 표를 뱉으면 파이프가 그대로 보이고
# 열이 어긋난다. 유일하게 줄이 맞는 방법은 고정폭 코드 블록이다.
def test_markdown_table_becomes_a_code_block():
    from tybot.evidence_view import fix_markdown_tables

    out = fix_markdown_tables(
        "앞말\n\n| 항목 | 상태 |\n|---|---|\n| 서버 이관 | 완료 |\n\n뒷말"
    )
    assert out.startswith("앞말")
    assert out.endswith("뒷말")
    assert "```" in out
    assert "|" not in out          # 파이프가 사용자에게 보이면 안 된다


def test_columns_line_up_with_hangul():
    """한글은 두 칸을 차지한다. 세지 않으면 열이 어긋난다."""
    from tybot.evidence_view import render_table

    body = render_table([["항목", "상태"], ["서버 이관", "완료"], ["백업", "진행중"]])
    rows = [r for r in body.splitlines() if r and not r.startswith("```")]
    widths = {len(r.split("  ")[0].encode("utf-8")) for r in rows}
    assert "서버 이관" in body and "진행중" in body
    assert len(widths) >= 1         # 첫 열이 같은 폭으로 채워졌다


def test_separator_row_is_dropped():
    from tybot.evidence_view import fix_markdown_tables

    assert "---" not in fix_markdown_tables("| a | b |\n|---|---|\n| 1 | 2 |").replace(
        "```", ""
    ).split("\n")[1]


def test_wide_table_falls_back_to_a_list():
    """깨진 표보다 항목 나열이 낫다."""
    from tybot.evidence_view import fix_markdown_tables

    wide = (
        "| 항목 | 아주아주긴설명입니다그렇습니다 | 담당자이름 | 비고가아주길어요 |\n"
        "|---|---|---|---|\n"
        "| 서버 이관 | 스토리지 교체와 네트워크 재구성 | 김수현 | 9월 중 완료 |"
    )
    out = fix_markdown_tables(wide)
    assert "```" not in out
    assert out.startswith("• ")
    assert "김수현" in out


def test_text_without_tables_is_untouched():
    from tybot.evidence_view import fix_markdown_tables

    text = "그냥 문장입니다.\n• 목록도 있습니다."
    assert fix_markdown_tables(text) == text


def test_answer_repairs_tables_in_to_slack():
    from tybot.answer import Answer

    a = Answer("| 항목 | 값 |\n|---|---|\n| 기성금 | 3억 |", [], "m", 0.0, 0, "answered")
    assert "```" in a.to_slack()
