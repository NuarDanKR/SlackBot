"""검토자 DM의 첨부 자동 변환 현황."""
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
def test_files_that_need_attention_come_first():
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
    assert "지원하지 않는 형식" in rows["사진.png"].urgency


# --- 원본 외부 전송 진입점이 생기지 않게 ------------------------------------
def test_the_status_view_has_no_approval_actions():
    blocks = av.blocks(_rows(FakeItem("F1", "가.png"), FakeItem("F2", "나.png")))

    assert not any(block["type"] == "actions" for block in blocks)


def test_the_header_says_conversion_is_automatic_and_originals_stay_local():
    blocks = av.blocks(_rows(FakeItem("F1", "가.png")))

    assert "자동" in blocks[0]["text"]["text"]
    assert "외부 LLM에 전송하지 않" in blocks[0]["text"]["text"]


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
