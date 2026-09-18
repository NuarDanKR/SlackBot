"""한 채널의 요약에 다른 채널의 줄이 섞이지 않는다 (B-61).

2026-09-17 QC 에서 `#팀-전산_abb155-전산팀장보고` 요약 검토 Canvas 에
`#팀-전산_abb155-공지` 의 내용이 들어갔다. 후보 조회도 원문 선택도 `channel_id` 로
거르므로, **그 `channel_id` 를 가진 문서가 다른 채널의 줄을 들고 있다**는 뜻이다.

`ArchiveStore.docs()` 는 문서를 `(워크스페이스, identity)` 로 묶어 한 문서로 합친다.
`identity` 는 진짜 `channel_id` → 표시명 순으로 떨어지므로, 표시명까지 내려간 문서가
엉뚱한 채널에 편입되면 그 채널의 근거가 오염된다. 이 파일은 그 경계를 고정한다.
"""
from __future__ import annotations

from pathlib import Path

from tybot import summary_review as sr
from tybot.archive.store import ArchiveStore


def _doc(root: Path, relative: str, *, channel: str, channel_id: str | None,
         version: int, line: str, quote: bool = True) -> None:
    """`quote=False` 는 v1 시절 모양이다 — 따옴표가 없으면 프론트매터 파서가
    `#` 를 주석으로 읽어 **표시명이 빈 문자열**이 된다(YAML 도 같게 읽는다)."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [
        "---",
        "workspace: tyit",
        f'channel: "{channel}"' if quote else f"channel: {channel}",
        "visibility: private",
        "acl:",
        "  - tyit",
        f"schema_version: {version}",
    ]
    if channel_id:
        head.append(f"channel_id: {channel_id}")
    if version == 2:
        head.append("source_date: 2026-09-17")
    head += ["---", "", "## 원문", "", f"> [2026-09-17 09:00] 홍길동: {line}", ""]
    path.write_text("\n".join(head) + "\n", encoding="utf-8")


def _v2(root: Path, channel_id: str, *, channel: str, line: str) -> None:
    _doc(
        root, f"workspaces/tyit/channels/{channel_id}/raw/2026-09-17.md",
        channel=channel, channel_id=channel_id, version=2, line=line,
    )


def test_two_real_channels_never_share_a_line(tmp_path):
    """가장 기본. 이게 깨지면 채널 권한 경계 전체가 무의미하다."""
    _v2(tmp_path, "C_NOTICE", channel="#팀-전산_abb155-공지", line="공지 채널의 줄")
    _v2(tmp_path, "C_REPORT", channel="#팀-전산_abb155-전산팀장보고",
        line="전산팀장보고의 줄")
    store = ArchiveStore(tmp_path)

    report = sr.channel_source(store, "tyit", "C_REPORT", "")
    notice = sr.channel_source(store, "tyit", "C_NOTICE", "")

    assert [line.text for line in report] == ["전산팀장보고의 줄"]
    assert [line.text for line in notice] == ["공지 채널의 줄"]


def test_a_shared_display_name_does_not_merge_two_real_channels(tmp_path):
    """채널명은 바뀐다. 옛 이름이 겹친다고 두 채널을 한 문서로 합치면 안 된다."""
    _v2(tmp_path, "C_NOTICE", channel="#팀-전산_abb155-공지", line="공지 채널의 줄")
    _v2(tmp_path, "C_REPORT", channel="#팀-전산_abb155-공지",
        line="이름만 같은 다른 채널의 줄")
    store = ArchiveStore(tmp_path)

    report = sr.channel_source(store, "tyit", "C_REPORT", "")

    assert [line.text for line in report] == ["이름만 같은 다른 채널의 줄"]


def test_summary_review_never_uses_a_legacy_doc_without_a_channel_id(tmp_path):
    """답변 호환용 이름 별칭은 요약 승인 후보의 채널 증명이 될 수 없다."""
    _v2(tmp_path, "C_NOTICE", channel="#팀-전산_abb155-공지", line="공지 채널의 줄")
    _v2(tmp_path, "C_REPORT", channel="#팀-전산_abb155-전산팀장보고",
        line="전산팀장보고의 줄")
    _doc(tmp_path, "channels/notice-legacy/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id=None, version=1,
         line="v1 문서에 남아 있던 공지 줄")
    store = ArchiveStore(tmp_path)

    report = sr.channel_source(store, "tyit", "C_REPORT", "")
    notice = sr.channel_source(store, "tyit", "C_NOTICE", "")

    assert [line.text for line in report] == ["전산팀장보고의 줄"]
    assert [line.text for line in notice] == ["공지 채널의 줄"]


def test_a_legacy_doc_whose_name_matches_nothing_joins_no_real_channel(tmp_path):
    """이름이 어느 채널과도 안 맞는 v1 문서는 **아무 채널에도 붙지 않는다.**

    붙일 곳을 못 찾았을 때 가까운 채널로 밀어 넣으면, 그 채널 검토자가 본 적 없는
    자료를 승인하게 된다.
    """
    _v2(tmp_path, "C_REPORT", channel="#팀-전산_abb155-전산팀장보고",
        line="전산팀장보고의 줄")
    _doc(tmp_path, "channels/orphan/2026-09-17.md",
         channel="#없어진-채널", channel_id=None, version=1, line="주인 없는 줄")
    store = ArchiveStore(tmp_path)

    report = sr.channel_source(store, "tyit", "C_REPORT", "")

    assert [line.text for line in report] == ["전산팀장보고의 줄"]


def test_an_unreadable_display_name_joins_no_channel_at_all(tmp_path):
    """이것이 2026-09-17 오염의 경로였다.

    `channel: #이름` 을 따옴표 없이 적으면 표시명이 빈 문자열이 된다. 예전에는 표시명이
    빈 문서들이 같은 별칭 키를 공유했고, 그 워크스페이스에 진짜 ID 가 하나뿐이면
    **서로 관계없는 문서 전부가 그 채널의 근거**가 됐다.
    """
    _v2(tmp_path, "C_REPORT", channel="#팀-전산_abb155-전산팀장보고",
        line="전산팀장보고의 줄")
    _doc(tmp_path, "channels/broken/2026-09-17.md",
         channel="#팀-전산_abb155-공지", channel_id=None, version=1,
         line="표시명을 읽지 못한 문서의 줄", quote=False)
    store = ArchiveStore(tmp_path)

    report = sr.channel_source(store, "tyit", "C_REPORT", "")

    assert [line.text for line in report] == ["전산팀장보고의 줄"]
