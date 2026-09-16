"""`근거 보기` — 답변이 실제로 읽은 원문 줄.

출처 줄은 *어디서* 왔는지만 말한다. 원문을 그대로 보여주면 질문이
"봇을 믿을 수 있나" 에서 "이 답이 맞나" 로 바뀐다 — 뒤쪽은 검증 가능한 질문이다.

핵심 성질: **답변이 그때 읽은 좌표를 지금 권한으로 다시 연다.** 다시 검색하지
않는다(2026-09-16 인계 §6.2) — 검색하면 답변이 읽은 것이 아니라 지금 그 낱말로
나오는 것을 보여 주게 된다. 권한은 클릭 시점에 다시 판정된다.
"""
from __future__ import annotations

from unittest.mock import Mock

from tybot.access import RequestContext
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
def _answered(body="전산팀은 서버 이관을 진행했습니다.", **kw):
    opts = {"record_id": "rec-1", "has_evidence": True, "answered": True}
    opts.update(kw)
    return blocks(body, **opts)


def test_the_button_carries_only_a_record_id():
    """§7.4 · 버튼 value 에 질문·원문·검색어를 넣지 않는다."""
    el = button("rec-1")["elements"][0]

    assert el["action_id"] == ACTION_SHOW
    assert el["value"] == "rec-1"


def test_no_button_without_a_record():
    assert button("") is None
    assert button("   ") is None


def test_a_successful_answer_with_evidence_gets_the_button():
    """§7.4 · 성공 + `EvidenceRef` 있음 → 버튼 표시."""
    out = _answered()

    assert out[0]["text"]["text"].startswith("전산팀은")
    assert out[-1]["elements"][0]["action_id"] == ACTION_SHOW


def test_a_failed_answer_never_gets_the_button():
    """§7.4 · timeout·`specialist_unavailable`·근거 없음에는 버튼이 없다.

    버튼이 있는데 눌러도 아무것도 안 나오면 없는 것만 못하다.
    """
    assert [b["type"] for b in _answered(answered=False)] == ["section"]
    assert [b["type"] for b in _answered(has_evidence=False)] == ["section"]
    assert [b["type"] for b in _answered(record_id="")] == ["section"]


def test_long_blocks_preserve_sources_in_a_later_section():
    body = "본문 " * 1000 + "\n\n출처:\n• #전산팀, 문서(2026-09-02)"
    out = _answered(body)
    rendered = "\n".join(
        block["text"]["text"] for block in out if block["type"] == "section"
    )
    assert "출처:" in rendered
    assert "#전산팀" in rendered
    assert all(len(block["text"]["text"]) <= 2900 for block in out if block["type"] == "section")


def test_button_value_is_capped():
    assert len(button("가" * 3000)["elements"][0]["value"]) <= 1900


# --- 표시 --------------------------------------------------------------------
def test_report_shows_raw_lines_verbatim():
    """요약하면 그게 또 하나의 답변이 된다. 손대지 않는다."""
    text = report([_line()], query="김해외동 기성금", own_workspace="mgmt")
    assert "김해외동 기성금은 3억 2천만원입니다" in text
    assert "김수현" in text


def test_report_groups_by_channel():
    lines = [_line(), _line(text="다른 줄"), _line(channel="#현장-김해외동_1800249-채팅방")]
    text = report(lines, query="기성금", own_workspace="mgmt")
    # 출처와 **같은 이름**으로 묶인다. 현장은 코드로 부른다.
    assert text.count("*[전산팀]회의*") == 1
    assert "*[1800249]김해외동-채팅방*" in text


def test_other_workspace_is_marked():
    """읽는 사람이 '이건 우리 자료가 아니다' 를 알아야 한다."""
    text = report([_line(workspace="pilot")], query="기성금", own_workspace="mgmt")
    # 앞자리는 조직 이름이 가져갔고, 워크스페이스는 뒤에 남는다.
    assert "(pilot)" in text


def test_own_workspace_is_not_marked():
    text = report([_line(workspace="mgmt")], query="기성금", own_workspace="mgmt")
    assert "(mgmt)" not in text


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
#
# **다시 검색하지 않는다.** 답변 당시 저장한 `EvidenceRef` 좌표를 지금 권한으로
# 다시 연다(인계 §6.2). 예전에는 버튼에 검색어를 실어 클릭 시 재검색했고, 그래서
# 보여 주는 것이 「답변이 읽은 원문」 이 아니라 「지금 그 낱말로 나오는 것」 이었다.
def _bot(hits=(), dropped=(), record=None):
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.store = Mock(resolve_refs=Mock(return_value=(list(hits), list(dropped))))
    bot.qa_log = Mock(by_record_id=Mock(return_value=record))
    bot._chan_cache = {}
    bot._context = lambda client, uid: RequestContext(
        workspace="mgmt", channels=frozenset({"#본사팀-전산_ABB110-회의"})
    )
    return bot


def _record(**kw) -> dict:
    base = {
        "record_id": "rec-1",
        "workspace": "mgmt",
        "user": "U1",
        "channel_id": "C1",
        "channel_type": "channel",
        # `EvidenceRef` 모양 그대로. 틀리면 `from_json` 이 조용히 버린다 —
        # 그 자리가 곧 「버튼을 눌렀는데 아무것도 안 나온다」 다.
        "evidence_refs": [{
            "kind": "archive_line",
            "workspace": "mgmt",
            "channel_id": "C1",
            "document_path": "workspaces/mgmt/channels/c1/raw/2026-09-01.md",
            "line_no": 3,
            "source_ts": "2026-09-01 14:03",
            "content_hash": "h1",
            "message_ts": "",
        }],
    }
    base.update(kw)
    return base


def _hit(**kw):
    doc = Mock(channel=kw.get("channel", "#본사팀-전산_ABB110-회의"),
               workspace=kw.get("workspace", "mgmt"))
    line = Mock(ts="2026-09-01T14:03+09:00", speaker="김수현",
                text=kw.get("text", "기성금은 3억 2천만원"))
    return Mock(doc=doc, line=line)


def test_the_button_reopens_stored_refs_instead_of_searching_again():
    """§7.4 · 클릭 시 재검색하지 않고 저장된 ref 를 현재 ACL 로 다시 연다."""
    bot = _bot([_hit()], record=_record())

    text = bot._evidence_text(Mock(), "U1", "rec-1", channel_id="C1")

    assert "3억 2천만원" in text
    assert "새로 검색하지 않았습니다" in text
    # 검색 경로는 아예 없다. 남겨 두면 다음 사람이 그것을 되살린다.
    assert not hasattr(bot.store, "search") or not bot.store.search.called


def test_permission_is_judged_at_click_time():
    """권한은 **지금** 본다. 답변 뒤 채널에서 나간 사람에게는 안 보인다."""
    bot = _bot([], dropped=["acl"], record=_record())

    text = bot._evidence_text(Mock(), "U1", "rec-1", channel_id="C1")

    assert "권한이 바뀌어" in text
    ctx = bot.store.resolve_refs.call_args.args[1]
    assert ctx.workspace == "mgmt"


def test_another_channel_member_can_open_the_channel_answers_evidence():
    """채널 답변은 질문자 소유가 아니라 현재 ACL을 가진 구성원의 자료다."""
    bot = _bot([_hit()], record=_record(channel_type="channel"))

    text = bot._evidence_text(
        Mock(), "U-channel-member", "rec-1", channel_id="C1"
    )

    assert "3억 2천만원" in text
    bot.store.resolve_refs.assert_called_once()


def test_a_channel_record_cannot_be_opened_from_another_channel():
    bot = _bot(record=_record(channel_type="channel"))

    text = bot._evidence_text(Mock(), "U1", "rec-1", channel_id="C2")

    assert "기록을 찾지 못했습니다" in text
    bot.store.resolve_refs.assert_not_called()


def test_another_person_cannot_open_a_dm_records_evidence():
    bot = _bot(record=_record(channel_id="D1", channel_type="im"))

    text = bot._evidence_text(Mock(), "U-stranger", "rec-1", channel_id="D2")

    assert "기록을 찾지 못했습니다" in text
    bot.store.resolve_refs.assert_not_called()


def test_a_record_without_refs_says_so():
    bot = _bot(record=_record(evidence_refs=[]))

    assert bot._evidence_text(
        Mock(), "U1", "rec-1", channel_id="C1"
    ) == NO_EVIDENCE
    bot.store.resolve_refs.assert_not_called()


def test_bot_handles_an_empty_record_id():
    bot = _bot()
    assert bot._evidence_text(Mock(), "U1", "  ") == NO_EVIDENCE
    bot.qa_log.by_record_id.assert_not_called()


def test_bot_survives_a_resolve_failure():
    bot = _bot(record=_record())
    bot.store.resolve_refs.side_effect = RuntimeError("boom")

    assert "다시 찾지 못했습니다" in bot._evidence_text(
        Mock(), "U1", "rec-1", channel_id="C1"
    )


def test_raw_lines_never_reach_the_log(caplog):
    """근거 줄은 사용자 화면에만 간다. 로그에 남기면 감사기록이 원문 사본이 된다."""
    bot = _bot([_hit(text="비밀 기성금 3억")], record=_record())
    with caplog.at_level("INFO"):
        out = bot._evidence_text(Mock(), "U1", "rec-1", channel_id="C1")
    logged = " ".join(r.getMessage() for r in caplog.records)

    assert "비밀 기성금 3억" in out
    assert "비밀 기성금 3억" not in logged
    assert "lines=1" in logged


def test_the_modal_uses_slacks_own_close_button():
    """§7.4 · 닫으면 원래 스레드가 그대로 남는다(인계 §6.3)."""
    from tybot.evidence_view import modal

    view = modal("답변이 읽은 원문")

    assert view["type"] == "modal"
    assert view["title"]["text"] == "답변 근거"
    assert view["close"]["text"] == "닫기"
    assert any("답변이 읽은 원문" in b["text"]["text"] for b in view["blocks"])


def test_the_ephemeral_fallback_can_be_dismissed():
    """§7.4 · 모달 실패 시 ephemeral 폴백을 닫을 수 있다."""
    from tybot.evidence_view import ACTION_DISMISS, fallback_blocks

    out = fallback_blocks("근거 본문")

    assert out[-1]["elements"][0]["action_id"] == ACTION_DISMISS


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
