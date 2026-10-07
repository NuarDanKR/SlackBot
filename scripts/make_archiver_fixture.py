#!/usr/bin/env python3
"""Archiver 정본 fixture 를 **실제 수집기로** 만든다.

사용:
  python scripts/make_archiver_fixture.py <나갈 디렉터리>

왜 스크립트인가 — Hermes 쪽(Node) 시험이 Archiver 정본을 읽어야 하는데, 그 경로와
파일 모양을 **손으로 적으면 안 된다.** 손으로 적은 fixture 는 쓴 사람이 생각한 모양을
고정할 뿐이고, 정본이 바뀌면 시험은 계속 통과한다 — 그때 reader 는 운영에서만 깨진다.

그래서 `archiving_bot.ShadowCollector` + `backfill.run` 을 그대로 돌린다. Slack
클라이언트만 가짜다. 경로는 `shadow_paths` 가, 줄 모양은 `archive.writer` 가, 첨부
정본은 `attachment_writer` 가 만든다 — 전부 운영이 쓰는 그 코드다.

나오는 것:
  <out>/<ws>/<channel-id>__<name>/archive/raw/<날짜>.md
  <out>/<ws>/<channel-id>__<name>/archive/attachments/<file-id>/<rev>.md
  <out>/<ws>/dm/<user-id>/archive/raw/<날짜>.md      ← reader 가 **읽으면 안 되는** 것
  <out>/other-ws/…                                   ← reader 가 **읽으면 안 되는** 것
  <out>/fixture.json                                 ← 시험이 읽는 좌표 정답표
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tybot import archiving_bot
from tybot.archive import backfill, backfill_adapter, files, shadow_paths, writer

WORKSPACE = "tyit"
OTHER_WORKSPACE = "pfteam"

# 한 채널은 **개명**을 겪는다(요구사항 4). 디렉터리 이름은 처음 이름으로 남고
# 채널 ID 가 정체성이다.
CH_FUNDS = ("C1000FUNDS", "#팀_자금(ABB540)_주간보고")
CH_FUNDS_RENAMED = "#팀_자금(ABB540)_주간보고-개편"
CH_SITE = ("C2000SITE0", "#현장_김해외동(180182)_채팅방")
CH_PRIVATE = ("C3000PRIV0", "#팀_인사(HRA100)_비공개")

DM_USER = "U9000PERSON"
DM_CHANNEL = "D9000PERSON"


class FakeSlack:
    """채널별 메시지·첨부를 돌려주는 가짜 Slack. **수집 경로는 진짜다.**"""

    def __init__(self, history: dict, names: dict):
        self._history = history
        self._names = names

    def conversations_info(self, *, channel):
        return {"channel": {"id": channel, "name": self._names[channel].lstrip("#"),
                            "is_member": True}}

    def conversations_history(self, *, channel, **_):
        return {"messages": self._history.get(channel, [])}

    def users_info(self, *, user):
        return {"user": {"name": user}}


def _msg(ts: str, user: str, text: str, files_=None) -> dict:
    row = {"ts": ts, "user": user, "text": text}
    if files_:
        row["files"] = files_
    return row


# 2026-09-28 09:00 KST = 1790899200 ; 하루 = 86400
TS = {
    "funds_d1_a": "1790899200.000100",
    "funds_d1_b": "1790899260.000100",
    "funds_d2_a": "1790985600.000100",   # 다음 날 — raw 파일이 갈린다
    "funds_d2_b": "1790985660.000100",
    "site_d1": "1790902800.000100",
    "priv_d1": "1790906400.000100",
    "other_d1": "1790910000.000100",
    "dm_d1": "1790913600.000100",
}

HISTORY = {
    CH_FUNDS[0]: [
        _msg(TS["funds_d1_a"], "U1", "금주 집행액은 12억원으로 확정되었습니다."),
        _msg(TS["funds_d1_b"], "U2", "추가 변동은 없습니다. 다음 주 보고에 반영합니다.", [{
            "id": "F100REPORT", "name": "자금계획.txt", "filetype": "txt", "size": 44,
            "url_private": "https://example.invalid/funds",
        }]),
        # 둘째 날. 시간순 병합(요구사항 5)이 날짜 경계를 넘는지 보려고 둔다.
        _msg(TS["funds_d2_a"], "U1", "집행 잔액은 3억원입니다."),
        _msg(TS["funds_d2_b"], "U3", "자금 수지 계획을 다시 올립니다."),
    ],
    CH_SITE[0]: [
        _msg(TS["site_d1"], "U4", "김해외동 현장 타워크레인 설치 완료했습니다."),
    ],
    CH_PRIVATE[0]: [
        _msg(TS["priv_d1"], "U5", "인사 평가 일정은 10월 둘째 주입니다."),
    ],
}

NAMES = {
    CH_FUNDS[0]: CH_FUNDS[1], CH_SITE[0]: CH_SITE[1], CH_PRIVATE[0]: CH_PRIVATE[1],
}


def _collect(root: Path, workspace: str, channels: list, history: dict, names: dict) -> None:
    config = archiving_bot.ArchiverWorkspace(
        workspace, "archiver-bot", "archiver-app", "T12345678", "U_MASTER",
        frozenset(cid for cid, _ in channels), separate_attachments=True,
    )
    collector = archiving_bot.ShadowCollector(config, root, layout="per-channel-v1")
    client = FakeSlack(history, names)
    targets = [backfill.Target(workspace, cid, name) for cid, name in channels]
    counts, state = backfill.run(
        client, targets, workspace=workspace,
        ingest=backfill_adapter.make_ingest(collector, client),
        save_cursor=lambda *_: None,
    )
    if str(state) not in {"JobState.SUCCEEDED", "succeeded"} and getattr(state, "name", "") != "SUCCEEDED":
        raise SystemExit(f"수집이 성공하지 않았습니다: {state} {counts}")


def build(out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # 첨부 본문은 실제 다운로드 대신 바이트를 꽂는다 — 변환·정본 쓰기는 진짜다.
    files.download_bytes = lambda *_: "자금 집행 계획: 12억원, 잔액 3억원.\n".encode()

    # 감사 기록·DB 는 fixture 에 필요 없다.
    archiving_bot.ingest_ack.advance = lambda **_: None
    archiving_bot.record_revision = lambda **_: 1

    _collect(out, WORKSPACE,
             [CH_FUNDS, CH_SITE, CH_PRIVATE], HISTORY, NAMES)

    # **개명.** 디렉터리는 그대로여야 한다(채널 ID 가 정체성).
    before = shadow_paths.channel_root(out, WORKSPACE, CH_FUNDS[0], CH_FUNDS[1])
    renamed = shadow_paths.channel_root(out, WORKSPACE, CH_FUNDS[0], CH_FUNDS_RENAMED)
    if before != renamed:
        raise SystemExit("개명이 디렉터리를 갈랐다 — fixture 전제가 깨졌다")

    # **다른 워크스페이스.** reader 가 읽으면 안 된다(요구사항 3).
    _collect(out, OTHER_WORKSPACE,
             [("C8000OTHER", "#pf_내부(PF001)_논의")],
             {"C8000OTHER": [_msg(TS["other_d1"], "U7", "PF 내부 논의입니다.")]},
             {"C8000OTHER": "#pf_내부(PF001)_논의"})

    # **DM.** reader 가 읽으면 안 된다(요구사항 3).
    dm_dir = shadow_paths.dm_archive_dir(out, WORKSPACE, DM_USER, DM_CHANNEL)
    writer.ingest(
        out, workspace=WORKSPACE, channel=writer.dm_channel(DM_USER),
        channel_id=DM_CHANNEL, dm_user=DM_USER, dm_directory=dm_dir,
        messages=[writer.IncomingMessage(
            datetime.fromtimestamp(float(TS["dm_d1"]), UTC), DM_USER,
            "개인 DM 에만 있는 문장입니다.", source_ts=TS["dm_d1"],
        )],
    )

    # **경로 판정이 틀린 날**을 fixture 로 만든다. reader 는 경로로 거르고 **내용으로
    # 한 번 더** 거르는데, 경로만으로도 DM·타 워크스페이스가 빠지므로 2차 방어는 어떤
    # 시험도 안 건드리게 된다. 안 건드리는 방어는 조용히 지워진다.
    #
    # 파일은 **실제 writer 가 만든 것을 옮긴다.** 내용을 손으로 적으면 「지금 writer 가
    # 내는 모양」 이 아니라 「옮긴 사람이 생각한 모양」 을 거르는 시험이 된다.
    funds_raw = shadow_paths.archive_dir(out, WORKSPACE, CH_FUNDS[0], CH_FUNDS[1]) / "raw"
    misplaced = []
    dm_src = next((dm_dir / "raw").glob("*.md"))
    shutil.copy2(dm_src, funds_raw / "2026-01-02.md")
    misplaced.append("dm")
    other_src = next(
        (out / OTHER_WORKSPACE).glob("C8000OTHER__*/archive/raw/*.md"))
    shutil.copy2(other_src, funds_raw / "2026-01-03.md")
    misplaced.append("other-workspace")

    manifest = {
        "workspace": WORKSPACE,
        "misplaced_in_channel": misplaced,
        "other_workspace": OTHER_WORKSPACE,
        "dm_user": DM_USER,
        "channels": [],
        "must_not_read": [],
    }
    for cid, name in [CH_FUNDS, CH_SITE, CH_PRIVATE]:
        archive = shadow_paths.archive_dir(out, WORKSPACE, cid, name)
        manifest["channels"].append({
            "channel_id": cid,
            "dir_name": archive.parent.name,
            "first_name": name,
            "raw": sorted(p.relative_to(out).as_posix() for p in (archive / "raw").glob("*.md")),
            "attachments": sorted(
                p.relative_to(out).as_posix()
                for p in (archive / "attachments").glob("*/*.md")
            ),
        })
    manifest["must_not_read"] = sorted(
        p.relative_to(out).as_posix()
        for p in out.rglob("*.md")
        if f"/{OTHER_WORKSPACE}/" in f"/{p.relative_to(out).as_posix()}"
        or "/dm/" in f"/{p.relative_to(out).as_posix()}"
    )
    manifest["renamed_to"] = CH_FUNDS_RENAMED
    (out / "fixture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) != 1:
        print(__doc__)
        return 1
    manifest = build(Path(argv[0]).resolve())
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
