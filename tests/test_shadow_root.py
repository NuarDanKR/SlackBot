"""흩어진 shadow root 를 **하나로 모으는** 규칙.

결정: 2026-09-29 작업지시서 §5 · §7-5.

여기서 보는 것은 세 가지다.

1. 옮긴 뒤에도 자료가 **같은가** — 해시를 다시 센다
2. 섞이면 안 되는 자리를 **거부하는가** — 운영 archive, 남의 워크스페이스
3. 다시 실행해도 **같은 결과인가** — 복사가 두 벌을 만들지 않는다
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tybot import archiving_bot
from tybot.archive import shadow_root


def _write(path, text: str = "원문 한 줄") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _old_root(tmp_path, workspace: str = "tyit"):
    """파일럿이 만든 모양 그대로. root 아래에 workspaces 가 한 층 더 있다."""
    base = tmp_path / "shadow" / workspace
    _write(base / "archive" / "workspaces" / workspace / "channels" / "C1__팀_자금"
           / "raw" / "2026-09-01.md")
    _write(base / "objects" / "workspaces" / workspace / "channels" / "C1"
           / "attachments" / "F1" / "보고서.pdf", "PDF 내용")
    return shadow_root.SourceRoot(workspace, base)


def _live(tmp_path):
    """운영 archive 자리. 절대경로 판정이 OS 마다 다르므로 tmp_path 로 만든다."""
    return tmp_path / "var" / "archive"


def _plan(tmp_path, sources):
    return shadow_root.plan(
        sources, tmp_path / "common", live_archive=_live(tmp_path),
    )


# --- 옮긴 뒤에도 같은가 ---------------------------------------------------------

def test_the_plan_keeps_the_relative_path(tmp_path):
    """상대경로가 바뀌면 `workspaces/<ws>/` 구분이 흐트러지고 권한이 따라 흔들린다."""
    planned = _plan(tmp_path, [_old_root(tmp_path)])

    assert [item.relative for item in planned.items] == [
        "workspaces/tyit/channels/C1__팀_자금/raw/2026-09-01.md",
        "workspaces/tyit/channels/C1/attachments/F1/보고서.pdf",
    ]
    assert not planned.blocked


def test_attachments_move_with_the_messages(tmp_path):
    """첨부는 archive 의 형제다. 원문만 옮기면 검색이 첨부만 못 찾는다."""
    planned = _plan(tmp_path, [_old_root(tmp_path)])

    assert {item.area for item in planned.items} == {"archive", "objects"}


def test_the_copy_matches_the_source(tmp_path):
    planned = _plan(tmp_path, [_old_root(tmp_path)])

    assert shadow_root.apply(planned)["copied"] == 2
    assert shadow_root.verify(planned) == []
    copied = (tmp_path / "common" / "archive" / "workspaces" / "tyit" / "channels"
              / "C1__팀_자금" / "raw" / "2026-09-01.md")
    assert copied.read_text(encoding="utf-8") == "원문 한 줄"


def test_verify_catches_a_copy_that_changed_afterwards(tmp_path):
    """계획 때 잰 값을 믿으면, 복사가 실패해도 계획이 맞다고 말한다."""
    planned = _plan(tmp_path, [_old_root(tmp_path)])
    shadow_root.apply(planned)
    planned.items[0].destination.write_text("바뀐 내용", encoding="utf-8")

    assert any("원본과 다릅니다" in problem for problem in shadow_root.verify(planned))


def test_verify_catches_a_missing_copy(tmp_path):
    planned = _plan(tmp_path, [_old_root(tmp_path)])
    shadow_root.apply(planned)
    planned.items[0].destination.unlink()

    assert any("복사본이 없습니다" in problem for problem in shadow_root.verify(planned))


def test_the_source_is_never_removed(tmp_path):
    """§5-6. 검증 기간 동안 옛 root 가 남아 있어야 되돌릴 수 있다."""
    source = _old_root(tmp_path)
    planned = _plan(tmp_path, [source])
    shadow_root.apply(planned)

    assert all(item.source.is_file() for item in planned.items)


# --- 다시 실행해도 같은가 -------------------------------------------------------

def test_a_second_run_copies_nothing(tmp_path):
    """복사가 두 벌을 만들면 같은 원문이 두 근거로 세어진다."""
    source = _old_root(tmp_path)
    shadow_root.apply(_plan(tmp_path, [source]))

    again = _plan(tmp_path, [source])

    assert again.copies == []
    assert [item.verdict for item in again.items] == ["same", "same"]
    assert not again.blocked
    assert shadow_root.apply(again)["copied"] == 0


def test_a_different_body_at_the_same_path_is_refused(tmp_path):
    """덮어쓰면 어느 쪽이 원문인지 알 수 없게 된다. 사람이 정해야 한다."""
    source = _old_root(tmp_path)
    shadow_root.apply(_plan(tmp_path, [source]))
    target = (tmp_path / "common" / "archive" / "workspaces" / "tyit" / "channels"
              / "C1__팀_자금" / "raw" / "2026-09-01.md")
    target.write_text("다른 원문", encoding="utf-8")

    planned = _plan(tmp_path, [source])

    assert planned.blocked
    assert any("다른 내용이 이미 있습니다" in line for line in planned.refusals)
    with pytest.raises(shadow_root.MigrationRefused):
        shadow_root.apply(planned)
    assert target.read_text(encoding="utf-8") == "다른 원문"


def test_a_blocked_plan_copies_nothing_at_all(tmp_path):
    """한 파일이 막혔다고 나머지만 옮기면, 절반만 옮겨진 채로 사람이 잊는다."""
    source = _old_root(tmp_path)
    _write(source.base / "archive" / "workspaces" / "mgmt" / "channels" / "C9"
           / "raw" / "2026-09-01.md")

    planned = _plan(tmp_path, [source])

    with pytest.raises(shadow_root.MigrationRefused):
        shadow_root.apply(planned)
    assert not (tmp_path / "common").exists()


# --- 섞이면 안 되는 자리 ---------------------------------------------------------

def test_another_workspace_under_this_root_is_refused(tmp_path):
    """root 를 잘못 적은 것이다. 그대로 옮기면 남의 ACL 아래로 들어간다."""
    source = _old_root(tmp_path)
    _write(source.base / "archive" / "workspaces" / "mgmt" / "channels" / "C9"
           / "raw" / "2026-09-01.md")

    planned = _plan(tmp_path, [source])

    assert planned.blocked
    assert any("다른 워크스페이스 자료가 있습니다" in line for line in planned.refusals)


def test_a_path_outside_workspaces_is_refused(tmp_path):
    """옮길 자리를 추측하지 않는다. 추측한 자리는 조용히 틀린다."""
    source = _old_root(tmp_path)
    _write(source.base / "archive" / "channels" / "옛-평면-구조.md")

    planned = _plan(tmp_path, [source])

    assert any("workspaces/ 아래가 아닙니다" in line for line in planned.refusals)


def test_two_roots_claiming_one_path_are_refused(tmp_path):
    """둘 다 옮기면 나중 것이 이긴다. 그 선택을 코드가 하면 안 된다."""
    first = _old_root(tmp_path, "tyit")
    second = shadow_root.SourceRoot("tyit", tmp_path / "shadow-2" / "tyit")
    _write(second.base / "archive" / "workspaces" / "tyit" / "channels" / "C1__팀_자금"
           / "raw" / "2026-09-01.md", "다른 사본")

    planned = _plan(tmp_path, [first, second])

    assert any("두 root 가 주장합니다" in line for line in planned.refusals)


def test_a_destination_holding_the_live_archive_is_refused(tmp_path):
    """shadow 자료가 운영 근거가 되면 되돌릴 수 없다(§8)."""
    live = _live(tmp_path)

    refusals = shadow_root.check_destination(live.parent / "archive", live)

    assert any("겹칩니다" in line for line in refusals)


def test_a_destination_that_shares_the_live_parent_is_refused(tmp_path):
    """archive 는 안 겹쳐도 첨부가 겹친다. objects/ 는 archive 의 형제다."""
    # 이름이 겹치지 않게 둔다. 겹치면 이 시험이 **다른 이유로** 통과한다.
    live = tmp_path / "var" / "live-archive"

    refusals = shadow_root.check_destination(live.parent, live)

    assert any("운영 첨부와 섞입니다" in line for line in refusals)
    assert not any("겹칩니다" in line for line in refusals)


def test_a_relative_destination_is_refused(tmp_path):
    """상대경로는 실행한 디렉터리에 따라 다른 자리를 가리킨다."""
    assert any(
        "절대경로" in line
        for line in shadow_root.check_destination(Path("archiver-shadow"), _live(tmp_path))
    )


def test_the_archive_root_is_below_the_common_base(tmp_path):
    """supervisor 가 받을 값이다. base 를 그대로 주면 첨부가 base 밖으로 나간다."""
    assert shadow_root.archive_root(tmp_path) == tmp_path / "archive"


# --- 런타임도 같은 규칙을 지킨다 -------------------------------------------------

def test_the_runtime_refuses_a_shadow_root_beside_the_live_archive(tmp_path):
    """도구만 막고 런타임이 허용하면, 사람은 도구를 건너뛰고 env 를 적는다."""
    env = {
        "ARCHIVE_DIR": str(tmp_path / "var" / "archive"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "var" / "archiver-shadow"),
    }

    with pytest.raises(archiving_bot.ArchiverConfigError, match="share a parent"):
        archiving_bot.shadow_archive_dir(env)

    env["ARCHIVER_SHADOW_DIR"] = str(tmp_path / "var" / "archiver-shadow" / "archive")
    assert archiving_bot.shadow_archive_dir(env).name == "archive"
