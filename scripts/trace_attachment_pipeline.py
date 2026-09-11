"""첨부 하나가 어디서 막혔는지 — 읽기 전용 추적.

설계: `docs/design/document-pipeline-trace-and-report-summary.md` §8

## 왜 이 도구가 필요한가
주간업무보고 HWP 가 올라온 채널에서 「여태까지 수집된 주간 보고 내용을 종합해줘」 라고
물었더니 봇이 「회의록이나 논의가 없다」 고 답했다. 파일은 실제로 있었다.

그 화면만으로는 원인을 고를 수 없다 — 수집 안 됨 / 변환 실패 / 원문 미반영 / 색인 지연 /
권한 / 랭킹 탈락이 **모두 같은 답**으로 보인다. 조치는 전부 다르다.

그래서 단계마다 따로 판정하고 **최초 실패 하나**를 가리킨다.

## 아무것도 고치지 않는다
읽기만 한다. 무엇을 할지는 사람이 정한다 — 판정을 보고 자동으로 재변환하면, 되살리면
안 되는 PII 차단 파일까지 되살리게 된다.

## 본문은 보이지 않는다
파일명과 상태·건수만 출력한다. 추출 본문과 OCR 결과는 출력하지 않는다.

## 쓰기

    python scripts/trace_attachment_pipeline.py --workspace tyit
    python scripts/trace_attachment_pipeline.py --workspace tyit --channel C0BQU...
    python scripts/trace_attachment_pipeline.py --name "주간보고"      # 이름 조각
    python scripts/trace_attachment_pipeline.py --failed-only --summary

종료 코드는 결과를 구분한다.

    0  전 단계 정상
    1  막힌 첨부가 있다
    2  입력·환경 오류
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot import attachment_trace as at
from tybot.envfile import load_env_file

EXIT_OK = 0
EXIT_STUCK = 1
EXIT_INPUT = 2


def _channel_lines(store, workspace: str, channel_id: str) -> list[str]:
    """이 채널 원문의 모든 줄. 첨부 표시를 여기서 찾는다.

    문서를 못 읽는 것과 첨부 줄이 없는 것은 다르다 — 못 읽으면 `archived` 를
    판정하면 안 되므로, 여기서는 읽은 것만 돌려주고 판정은 호출자가 한다.
    """
    lines: list[str] = []
    for doc in store.source_docs():
        if doc.workspace != workspace:
            continue
        if channel_id and (doc.channel_id or "") != channel_id:
            continue
        lines += [ln.text for ln in doc.raw_lines]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="첨부 처리 단계 추적 — 읽기만 한다",
    )
    ap.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    ap.add_argument("--workspace", default="", help="워크스페이스 키")
    ap.add_argument("--channel", default="", help="Slack 채널 ID")
    ap.add_argument("--file-id", default="", help="Slack 파일 ID")
    ap.add_argument("--name", default="", help="파일명 조각(부분 일치)")
    ap.add_argument("--failed-only", action="store_true", help="막힌 것만 출력")
    ap.add_argument("--summary", action="store_true", help="건별 대신 집계만")
    args = ap.parse_args(argv)

    load_env_file()

    from tybot.archive.store import ArchiveStore

    archive = pathlib.Path(args.archive)
    if not archive.exists():
        print(f"아카이브 경로가 없다: {archive}")
        return EXIT_INPUT

    staged = at.staged_attachments(
        archive, workspace=args.workspace, channel_id=args.channel
    )
    if args.file_id:
        staged = [(p, m) for p, m in staged if str(m.get("slack_file_id")) == args.file_id]
    if args.name:
        needle = args.name.lower()
        staged = [(p, m) for p, m in staged if needle in str(m.get("name", "")).lower()]

    if not staged:
        print("조건에 맞는 첨부가 없다.")
        print("첨부가 있는데 0건이면 수집 자체를 못 한 것이다 — `/수집상태` 를 먼저 본다.")
        return EXIT_INPUT

    store = ArchiveStore(archive)

    # 같은 파일이 **여러 채널에 공유**되면 채널마다 따로 격리된다. 그건 모호함이
    # 아니라 같은 파일이다 — 전체에서 이름을 세면 그 경우가 「같은 이름 2건」 으로
    # 잘못 잡힌다(2026-09-11 실측). 그래서 **채널 안에서, 서로 다른 파일 ID 만** 센다.
    seen: dict[tuple[str, str, str], set[str]] = {}
    located: list[tuple[Path, dict, str, str]] = []
    for meta_path, meta in staged:
        parts = meta_path.parts
        workspace = parts[parts.index("workspaces") + 1]
        channel_id = parts[parts.index("channels") + 1]
        key = (workspace, channel_id, str(meta.get("name") or ""))
        seen.setdefault(key, set()).add(str(meta.get("slack_file_id") or meta_path.parent.name))
        located.append((meta_path, meta, workspace, channel_id))

    line_cache: dict[tuple[str, str], list[str]] = {}
    traces: list[at.AttachmentTrace] = []
    for meta_path, meta, workspace, channel_id in located:
        key = (workspace, channel_id)
        if key not in line_cache:
            line_cache[key] = _channel_lines(store, workspace, channel_id)
        name_key = (workspace, channel_id, str(meta.get("name") or ""))
        traces.append(at.trace(
            meta,
            meta_path,
            workspace=workspace,
            channel_id=channel_id,
            doc_lines=line_cache[key],
            same_name=len(seen[name_key]),
        ))

    counts = at.summarize(traces)
    shown = [t for t in traces if not args.failed_only or not t.ok]

    if not args.summary:
        for got in shown:
            print(f"[{got.workspace}/{got.channel_id}]")
            print(got.report())
            print()
    print(f"=== 첨부 {len(traces)}건 — {at.summary_line(counts)}")
    if args.failed_only and not shown:
        print("막힌 첨부가 없다.")

    # 색인·검색·답변 선택은 이 도구가 판정하지 않는다. 모른다고 말해 둔다 —
    # 「전부 정상」 으로 읽히면 그 뒤 단계를 아무도 안 본다.
    print("색인·검색 가능·답변 선택 단계는 DB 와 요청 문맥이 필요해 여기서 판정하지 않는다"
          " (설계 §8).")
    return EXIT_OK if all(t.ok for t in traces) else EXIT_STUCK


if __name__ == "__main__":
    raise SystemExit(main())
