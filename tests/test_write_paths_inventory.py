"""운영 원문에 쓰는 **모든 경로**가 소유권 판정을 지나는가.

결정: 2026-09-30 오너 — 남은 쓰기 경로를 확인하라.

## 왜 목록을 시험이 드나

문지기는 부르는 곳이 있어야 일한다. `owns_write()` 가 2026-09-25 부터 있었는데도
콘솔에서 인수한 채널에 Master 가 계속 쓴 이유가 그것이다 — 규칙은 있고 부르는 곳이
없었다.

새 쓰기 경로는 앞으로도 생긴다. 그때 **아무도 이 판정을 기억하지 못하면** 같은 일이
반복되고, 그 반복은 오류를 내지 않는다. 그래서 목록을 여기 박는다. 새 `writer.ingest`
호출부가 생기면 이 시험이 깨지고, 깨진 사람이 셋 중 하나로 분류해야 한다.

| 분류 | 뜻 | 문지기 |
|---|---|---|
| `gated` | Slack 에서 **새 대화**를 가져와 운영 원문에 넣는다 | **필요** |
| `no-new-conversation` | 이미 아카이브에 있는 줄을 채운다. Slack 을 안 부른다 | 불필요 |
| `out-of-scope` | 채널 소유권과 무관(DM·이행 도구·벤치마크) | 불필요 |
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: `writer.ingest(` 를 부르는 파일과 그 분류. **값은 사람이 정한다.**
#:
#: 자동으로 판정하지 않는 이유: 「Slack 을 부르나」 는 import 만 보고 알 수 없고,
#: 틀리면 조용히 통과한다. 사람이 한 번 보고 적는 편이 낫다.
WRITE_PATHS: dict[str, str] = {
    # -- Master 가 Slack 에서 새 대화를 가져오는 곳 --------------------------
    "src/tybot/slack/pilot.py": "gated",
    "src/tybot/collect.py": "gated",
    "scripts/backfill_channel_history.py": "gated",
    "scripts/sync_channel_files.py": "gated",
    # -- 이미 있는 줄을 채우는 곳. Slack 을 안 부른다 ------------------------
    #
    # 여기를 막으면 인수 **전에** 들어온 첨부가 영영 변환 안 된 채 남는다. 그건
    # 중복이 아니라 누락이고, 누락 쪽이 나쁘다.
    "scripts/convert_staged_attachments.py": "no-new-conversation",
    "scripts/drain_conversion_queue.py": "no-new-conversation",
    # -- 소유권과 무관 -------------------------------------------------------
    #
    # Archiver 는 자기 root(shadow)에 쓴다. 운영 경로를 열 때는
    # `write_owner.archiver_may_write_live()` 를 지나야 한다(아직 안 열렸다).
    "src/tybot/archiving_bot.py": "out-of-scope",
    # v1 -> v2 이행. 채널 단위가 아니라 아카이브 전체를 옮긴다.
    "src/tybot/archive/migrate.py": "out-of-scope",
    # 레이아웃 실측. 임시 경로에만 쓴다.
    "scripts/archive_layout_bench.py": "out-of-scope",
}

#: 문지기를 지났다고 인정하는 호출. 이름이 바뀌면 여기도 바뀌어야 한다.
GATE_CALL = re.compile(r"master_may_write\s*\(")

SEARCH_DIRS = ("src", "scripts")


def _sources() -> list[Path]:
    return [
        path
        for directory in SEARCH_DIRS
        for path in sorted((REPO / directory).rglob("*.py"))
    ]


def calls_ingest(source: str) -> bool:
    """이 소스가 `writer.ingest(...)` 를 **실제로 부르나.**

    정규식으로 세지 않는다. 이 저장소는 설명을 길게 쓰기 때문에 주석·독스트링에
    `writer.ingest()` 가 여러 번 나오고, 글자만 세면 이 시험이 「무엇이 쓰나」 가
    아니라 「무엇이 언급하나」 를 보게 된다. 구문 나무는 그 둘을 구분한다.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "ingest"
            and isinstance(func.value, ast.Name)
            and func.value.id == "writer"
        ):
            return True
    return False


def _callers() -> set[str]:
    return {
        path.relative_to(REPO).as_posix()
        for path in _sources()
        if calls_ingest(path.read_text(encoding="utf-8"))
    }


def test_every_write_path_is_classified():
    """새 경로가 생기면 여기서 걸린다. 분류는 사람이 한 번 보고 적는다."""
    assert _callers() == set(WRITE_PATHS)


def test_the_gated_paths_actually_call_the_gate():
    """목록에 `gated` 라고 적는 것만으로는 아무것도 막지 못한다."""
    for name, kind in sorted(WRITE_PATHS.items()):
        if kind != "gated":
            continue
        source = (REPO / name).read_text(encoding="utf-8")

        assert GATE_CALL.search(source), name


def test_the_detector_finds_a_real_call():
    """탐지기 자체의 양성 시험. 아무것도 못 찾으면 목록이 비고 시험이 통과한다."""
    assert calls_ingest("r = writer.ingest(root, workspace='w')")
    assert calls_ingest("def f():\n    return writer.ingest(a)")


def test_the_detector_ignores_a_mention():
    """이 저장소는 설명이 길다. 주석·독스트링을 세면 목록이 언급으로 채워진다."""
    assert not calls_ingest("# writer.ingest() 가 호출한다")
    assert not calls_ingest('"""`writer.ingest()` 뒤에 그 파일의 줄이 있는지 본다."""')
    assert not calls_ingest("other.ingest(root)")


def test_the_gate_detector_is_not_satisfied_by_a_mention():
    """주석에 이름만 적어 두고 안 부르는 상태를 통과시키면 안 된다."""
    assert GATE_CALL.search("verdict = owner.master_may_write(channel_id)")
    assert not GATE_CALL.search("# master_may_write 를 나중에 붙인다")


def test_the_classification_vocabulary_is_closed():
    """오타로 새 분류가 생기면 그 경로는 아무 시험도 안 받는다."""
    assert set(WRITE_PATHS.values()) <= {
        "gated", "no-new-conversation", "out-of-scope",
    }


def test_the_converters_do_not_read_slack():
    """`no-new-conversation` 이라는 분류의 근거. 바뀌면 분류부터 다시 해야 한다."""
    for name, kind in sorted(WRITE_PATHS.items()):
        if kind != "no-new-conversation":
            continue
        source = (REPO / name).read_text(encoding="utf-8")

        for forbidden in ("slack_sdk", "WebClient", "conversations_history"):
            assert forbidden not in source, f"{name}: {forbidden}"
