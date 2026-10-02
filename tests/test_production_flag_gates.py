"""운영 차단 조건이 **실제로 무언가를 막는가.**

`production_blockers` 는 네 스위치가 켜져야 운영 전환을 허용한다. 그런데 스위치가
막아야 할 동작이 그 스위치를 **안 보고** 돌면, 차단 조건은 통과 여부만 바뀌고
동작은 그대로다. 그 상태의 체크리스트는 체크됐다는 사실 말고 아무것도 보증하지
않는다 — 그리고 사람은 체크된 것을 보증으로 읽는다.

여기 시험은 두 가지를 본다.

1. 필수 스위치마다 **런타임에서 읽는 자리가 있는가**
2. 첨부 ACK 가 「변환 중」·「변환 불가」·「원본 저장 실패」 를 **구분해 말하는가**

둘 다 지금은 실패한다. 고치는 길은 두 갈래이고 어느 쪽이든 이 시험이 통과한다 —
스위치에 진짜 게이트를 붙이거나, 보증하지 못하는 필수 조건을 걷어내거나.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tybot.archive.archiving_state import (
    REQUIRED_PRODUCTION_FLAGS,
    IngestProgress,
    IngestState,
    searchable_claim,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "tybot"


def _catalog_spans(tree: ast.Module) -> list[ast.AST]:
    """모듈 최상위의 **이름 목록** 값들.

    `REQUIRED_PRODUCTION_FLAGS` · `KNOWN_FLAGS` 같은 ALL_CAPS 목록은 스위치를
    **세는** 자리지 **읽는** 자리가 아니다. 여기 들어 있다는 사실만으로 「연결됐다」
    고 보면 이 시험이 아무것도 못 잡는다.
    """
    spans: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if any(name.isupper() for name in names) and node.value is not None:
            spans.append(node.value)
    return spans


def _runtime_reads(flag: str) -> list[str]:
    """그 이름을 **값으로 쓰는** 자리들. `파일:줄` 로 돌려준다.

    문자열 **상수가 그 이름과 같은** 노드만 센다. 주석·독스트링에 이름이 나오는
    것은 설명이지 동작이 아니므로 여기 안 걸린다(독스트링은 이름과 같지 않다).
    """
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - 읽히는 소스만 본다
            continue
        catalog = {
            id(node)
            for value in _catalog_spans(tree)
            for node in ast.walk(value)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == flag
                and id(node) not in catalog
            ):
                found.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno}")
    return found


#: 2026-10-02 조사에서 **읽는 자리가 없던** 필수 스위치들. 오너 결정 전까지
#: `xfail(strict)` 로 둔다 — 고치면 XPASS 가 나면서 이 목록을 지우라고 알려 준다.
#: 그냥 지우면 구멍이 기록에서 사라지고, 사라진 구멍은 다시 생긴다.
UNWIRED = {
    "preserve_edit_delete": "수정·삭제 줄을 운영 원문에 쓰는 동작이 스위치를 안 본다",
    "require_attachment_ack": "첨부 ACK 요구가 스위치가 아니라 인자 기본값이다",
    "revision_reader_ready": "revision reader 가 스위치와 무관하게 늘 돈다",
}


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param(
            name,
            marks=pytest.mark.xfail(strict=True, reason=UNWIRED[name])
            if name in UNWIRED
            else (),
        )
        for name in REQUIRED_PRODUCTION_FLAGS
    ],
)
def test_every_required_production_flag_is_read_by_runtime_code(flag: str) -> None:
    """필수 조건이면 **꺼져 있을 때 달라지는 것**이 있어야 한다.

    읽는 자리가 없으면 켜도 아무 일이 안 일어나고 꺼도 아무 일이 안 멈춘다. 그
    스위치가 요구하는 「준비됐다」 는 사람의 기억일 뿐이고, 기억은 반 년 뒤에
    틀린다. 그래서 목록에 있는 것과 연결된 것을 여기서 대조한다.

    **이 시험의 한계**: 읽는 자리가 있다는 것까지만 본다. 읽고 나서 제대로
    분기하는지는 그 기능의 시험이 본다.
    """
    reads = _runtime_reads(flag)
    assert reads, (
        f"`{flag}` 는 운영 차단 조건인데 런타임에서 읽는 자리가 없습니다. "
        "이름 목록에만 있습니다 — 켜도 동작이 안 바뀌고, 그 이름이 약속하는 "
        "보증은 아무 데서도 지켜지지 않습니다. 게이트를 붙이거나 "
        "REQUIRED_PRODUCTION_FLAGS 에서 빼세요."
    )


def test_the_scan_does_not_count_a_name_list_as_a_reader() -> None:
    """이 시험이 헛돌지 않는다는 증거.

    목록에 이름을 적는 것만으로 통과하면, 위 시험은 「목록에 있나」 를 묻는 셈이
    되고 그건 항상 참이다.
    """
    tree = ast.parse(
        'KNOWN = ("demo_flag",)\n'
        'OTHER: frozenset = frozenset({"demo_flag"})\n'
    )
    catalog = {
        id(node) for value in _catalog_spans(tree) for node in ast.walk(value)
    }
    constants = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "demo_flag"
    ]
    assert len(constants) == 2
    assert all(id(node) in catalog for node in constants)


def test_a_call_argument_counts_as_a_reader() -> None:
    """반대쪽도 본다 — 진짜 읽는 자리는 걸러지면 안 된다."""
    tree = ast.parse('def f(flags):\n    return flags.get("demo_flag", False)\n')
    catalog = {
        id(node) for value in _catalog_spans(tree) for node in ast.walk(value)
    }
    constants = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "demo_flag"
    ]
    assert len(constants) == 1
    assert id(constants[0]) not in catalog


# ---------------------------------------------------------------------------
# 첨부 ACK — 세 가지 결말을 구분해 말하는가
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="세 결말이 PARTIAL 하나로 모이고 사유는 문장까지 오지 못한다",
)
def test_attachment_outcomes_are_told_apart() -> None:
    """「변환 중」·「변환 불가」·「원본 저장 실패」 는 **다른 말**이어야 한다.

    셋은 사람이 할 일이 다르다.

    - **변환 중** — 기다리면 된다
    - **변환 불가**(`unsupported`) — 기다려도 안 된다. 본문 검색에는 영영 안
      잡히고, 대신 원본이 보관돼 있다. 「아직」 이라고 하면 사람은 내일 다시 와서
      또 「아직」 을 본다
    - **원본 저장 실패** — 되돌릴 수 없는 쪽이다. 원본이 없으면 나중에 다시
      만들지도 못한다. 사람이 「기록했습니다」 를 읽고 Slack 원본을 지우면 그걸로
      끝이다. 이 하나만은 **다시 올려 달라고 말해야 한다**

    지금은 셋 다 `PARTIAL` 로 들어가고, 사유(`error_code`)는 표에만 적힌다 —
    `ingest_ack.read()` 의 SELECT 에 그 열이 아예 없어서 문장을 만드는 쪽까지
    오지 못한다. 그래서 세 문장이 글자 하나까지 같다.
    """
    # `archiving_bot.ingest_message` 가 세 경우에 실제로 남기는 것. 사유는
    # `error_code` 로 표에 들어가지만 **상태와 숫자는 똑같다.**
    recorded = {
        "attachment-not-searchable": IngestProgress(IngestState.PARTIAL, 1, 0),
        "attachment-unsupported": IngestProgress(IngestState.PARTIAL, 1, 0),
        "attachment-original-missing": IngestProgress(IngestState.PARTIAL, 1, 0),
    }
    claims = {
        cause: searchable_claim(progress, require_ack=True)
        for cause, progress in recorded.items()
    }

    assert len(set(claims.values())) == 3, (
        "첨부 결말 세 가지가 같은 문장으로 나옵니다: "
        f"{sorted(set(claims.values()))}. 사유는 {sorted(claims)} 로 나뉘는데 "
        "문장은 하나입니다 — 기다리면 되는 것과 기다려도 안 되는 것과 원본을 "
        "잃은 것이 구분되지 않습니다."
    )


def test_the_claim_cannot_see_why_it_is_partial() -> None:
    """위 시험이 왜 실패하는지를 못 박는다.

    문장을 만드는 입력(`IngestProgress`)에 사유가 **없다.** 호출부에서 고칠 수
    있는 문제가 아니라 자료 구조의 문제다 — 사유를 실어 나르지 않으면 어느
    호출부도 구분해 말할 수 없다.
    """
    assert not hasattr(IngestProgress(IngestState.PARTIAL), "error_code")
