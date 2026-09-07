"""첨부 검수 화면 (`/첨부`).

승인은 **원본을 벤더에 보내도 되는가** 를 정하는 일이다. 서버 목록에는 이름만 있어
무엇을 내보내는지 모르는 채로 승인하게 된다 — 그건 게이트가 아니라 형식이다.
파일이 보이는 자리는 그것이 올라온 채널이고, 그래서 Slack 에서 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from tybot import attachment_view as av


@dataclass
class FakeItem:
    file_id: str
    name: str
    filetype: str = ""
    size: int = 240 * 1024


def _rows(*items, extracted: set[str] | None = None) -> list[av.Row]:
    return av.rows_for(list(items), extracted or set())


# --- 무엇부터 봐야 하는지 ----------------------------------------------------
def test_files_that_cannot_be_read_without_approval_come_first():
    """변환된 파일은 이미 텍스트로 답변에 쓰인다 — 승인이 급하지 않다.

    변환이 안 되는 것은 **승인 없이는 내용을 알 방법이 없다.** 사람의 시간을
    거기 먼저 쓰게 한다.
    """
    rows = _rows(
        FakeItem("F1", "요건정의서.xlsx"),
        FakeItem("F2", "스크린샷.png"),
        extracted={"요건정의서.xlsx"},
    )

    assert [r.name for r in rows] == ["스크린샷.png", "요건정의서.xlsx"]


def test_the_urgency_says_why():
    """「승인 필요」 만 적으면 왜인지 모른다. 세 상태가 서로 다른 조치를 부른다."""
    rows = {
        r.name: r
        for r in _rows(
            FakeItem("F1", "표.xlsx"),
            FakeItem("F2", "보고서.pdf"),
            FakeItem("F3", "사진.png"),
            extracted={"표.xlsx"},
        )
    }

    assert "이미 사용 중" in rows["표.xlsx"].urgency
    assert "변환 실패" in rows["보고서.pdf"].urgency
    assert "변환 불가" in rows["사진.png"].urgency


# --- Block Kit 이 조용히 사라지지 않게 ---------------------------------------
def test_every_button_carries_a_value():
    """버튼 `value` 를 비우면 Slack 이 **메시지를 통째로 거부한다.**

    오류가 아니라 「아무 일도 안 일어남」 으로 나타난다(실제로 겪었다).
    """
    blocks = av.blocks(_rows(FakeItem("F1", "가.png"), FakeItem("F2", "나.png")))

    buttons = [
        element
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
    ]

    assert buttons, "버튼이 없다"
    for button in buttons:
        assert button["value"], f"value 가 비었다: {button['action_id']}"


def test_approve_asks_for_confirmation():
    """승인은 되돌릴 수 없는 쪽이다 — 보낸 것은 되돌아오지 않는다."""
    blocks = av.blocks(_rows(FakeItem("F1", "가정산서.xlsx")))

    approve = next(
        element
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
        if element["action_id"] == av.ACTION_APPROVE
    )

    assert "confirm" in approve
    assert "되돌아오지 않습니다" in approve["confirm"]["text"]["text"]


def test_reject_does_not_ask():
    """반려는 안전한 쪽이다. 확인을 물으면 사람이 확인창에 무뎌진다."""
    blocks = av.blocks(_rows(FakeItem("F1", "가.png")))

    reject = next(
        element
        for block in blocks
        if block["type"] == "actions"
        for element in block["elements"]
        if element["action_id"] == av.ACTION_REJECT
    )

    assert "confirm" not in reject


def test_the_header_warns_what_approval_means():
    """무엇을 하는 버튼인지 화면이 말해야 한다."""
    blocks = av.blocks(_rows(FakeItem("F1", "가.png")))

    assert "LLM 제공자에게 전달" in blocks[0]["text"]["text"]
    assert "개인정보" in blocks[0]["text"]["text"]


def test_a_long_list_says_how_many_are_left(monkeypatch):
    """Slack 이 긴 메시지를 자른다. 잘린 것을 모르면 다 처리한 줄 안다."""
    monkeypatch.setattr(av, "MAX_ITEMS", 2)
    rows = _rows(*[FakeItem(f"F{i}", f"파일{i}.png") for i in range(5)])

    blocks = av.blocks(rows)

    tail = blocks[-1]
    assert tail["type"] == "context"
    assert "외 3건" in tail["elements"][0]["text"]


# --- 본문은 담지 않는다 ------------------------------------------------------
def test_the_view_never_carries_file_content():
    """검수 화면은 이름과 상태만 보인다. 본문을 실으면 그것이 로그·기록에 남는다."""
    import inspect

    source = inspect.getsource(av)

    for leaked in ("read_bytes", "read_text", "object_path", "open("):
        assert leaked not in source, f"화면이 파일 내용을 만진다: {leaked}"
