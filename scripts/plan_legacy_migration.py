"""기존 TYBot 아카이브를 새 채널별 구조로 옮기는 **계획과 검증 보고서.**

설계: `docs/design/legacy-archive-migration.md`

이 스크립트는 **아무것도 옮기지 않는다.** 원본을 읽고 계획을 세워 보고서를 낼 뿐이다.
`--apply` 같은 스위치가 없는 것이 의도다 — 계획을 세우는 명령과 옮기는 명령이 같으면,
보기만 하려던 실행이 옮겨 버리는 날이 온다.

서버에서:

```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/plan_legacy_migration.py \\
    --source /var/lib/tybot/archive \\
    --destination /var/lib/tybot/archiver-shadow/archive \\
    --live-archive /var/lib/tybot/archive \\
    --collected /var/lib/tybot/archiver-shadow/archive \\
    --json > /tmp/legacy-migration-plan.json
```

`--source` 와 `--live-archive` 가 같아도 된다. 원본은 **읽기만** 하고, 목적지가 그
경로와 겹치면 거절한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tybot.archive import legacy_migrate


def _human(report: legacy_migrate.Report) -> str:
    data = report.as_json()
    content, rights = data["content"], data["permissions"]
    source, files = data["provenance"], data["attachments"]
    lines = [
        f"원본      {data['source']}",
        f"목적지    {data['destination']}",
        "",
        "## 내용",
        f"  경로 배정 가능 문서 {content['pathAssignableDocuments']}건"
        f" · 막힌 문서 {content['blockedDocuments']}건"
        f" · 못 읽은 문서 {content['unreadable']}건",
        "    (경로를 정할 수 있다는 뜻입니다. 안전하게 옮길 수 있는 수가 아닙니다)",
        f"  raw 전체 {content['rawLines']}줄 (이관 가능량 아님) — 갈래별:",
        f"    나머지(사람 대화로 추정) {content['residualLines']}줄",
        f"    첨부 참조                {content['attachmentReferenceLines']}줄",
        f"    첨부 추출 본문           {content['attachmentBodyLines']}줄  ← 파생 자료",
        f"    수정·삭제 이력           {content['revisionLines']}줄",
        f"    캔버스                   {content['canvasLines']}줄",
        "",
        f"  좌표가 맞은 메시지 {content['matchedMessages']}건"
        f" — 그 메시지에 속한 옛 줄 {content['rawLinesInMatchedMessages']}"
        f"(그중 나머지 {content['residualLinesInMatchedMessages']})",
        "",
        f"  ~ 옮길 나머지 **추정치** {content['residualNewLinesEstimate']}줄"
        f" = 나머지 {content['residualLines']}"
        f" - 좌표가 맞은 나머지 {content['residualLinesInMatchedMessages']}",
        f"    {content['estimateCaveat']}",
        "",
        f"  {content['note']}",
        "",
        "## 권한",
        f"  공개 {rights['public']}건 · 비공개 {rights['private']}건",
        f"  ACL 이 빈 문서 {rights['emptyAcl']}건 · 타 워크스페이스 공유 {rights['crossWorkspaceShared']}건",
        f"  DM 문서 {rights['dmDocumentsLeftBehind']}건은 채널 구조 밖이라 두고 갑니다(열지 않았습니다)",
        "",
        "## 출처",
        f"  좌표 있는 줄 {source['coordinatedLines']} · 좌표 없는 줄 {source['uncoordinatedLines']}",
        f"  그중 나머지 {source['residualLines']}줄"
        f" = 좌표 있음 {source['residualCoordinatedLines']}"
        f" + 좌표 없음 {source['residualUncoordinatedLines']}",
        f"  채널 ID 가 없어 막힌 문서 {source['documentsWithoutChannelId']}건",
        f"  schema v1 {source['schemaV1']}건 · v2 {source['schemaV2']}건",
        f"  {source['note']}",
        "",
        "## 첨부",
        f"  첨부 참조 줄 {files['referenceLines']} · 추출 본문 줄 {files['bodyLines']}",
        f"  파일 ID 로 이어진 참조 {files['identifiedFiles']}",
        f"  파일 ID 없이 이름으로만 이어진 줄 {files['linesWithoutFileId']}",
    ]
    if data["refusals"]:
        lines += ["", "## 거부"] + [f"  {item}" for item in data["refusals"]]
    if data["blockedDocuments"]:
        lines += ["", "## 막힌 문서"]
        lines += [
            f"  {item['path']}\n    {item['reason']}"
            for item in data["blockedDocuments"][:20]
        ]
        if len(data["blockedDocuments"]) > 20:
            lines.append(f"  … 그 외 {len(data['blockedDocuments']) - 20}건")
    if data["unreadable"]:
        lines += ["", "## 못 읽은 문서"]
        lines += [f"  {item['path']}: {item['reason']}" for item in data["unreadable"][:20]]
    lines += ["", data["note"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="옛 아카이브 루트(읽기만 한다)")
    parser.add_argument("--destination", required=True, help="새 채널별 구조의 루트")
    parser.add_argument(
        "--live-archive", required=True,
        help="운영 ARCHIVE_DIR. 목적지가 여기와 겹치는지 판정하는 데만 쓴다",
    )
    parser.add_argument(
        "--collected", default="",
        help="소급·그림자가 이미 가져온 아카이브 루트. 주지 않으면 중복을 대조하지 않는다",
    )
    parser.add_argument("--json", action="store_true", help="보고서를 JSON 으로 출력")
    args = parser.parse_args(argv)

    collected = (
        legacy_migrate.collected_coordinates(args.collected) if args.collected else None
    )
    report = legacy_migrate.plan(
        args.source, args.destination,
        live_archive=args.live_archive, collected=collected,
    )
    if args.json:
        print(json.dumps(report.as_json(), ensure_ascii=False, indent=2))
    else:
        print(_human(report))
        if not args.collected:
            print(
                "\n경고: --collected 를 주지 않아 소급 결과와 대조하지 않았습니다."
                " 겹치는 줄 수가 0 으로 보입니다."
            )
    return 2 if report.refusals else 0


if __name__ == "__main__":
    raise SystemExit(main())
