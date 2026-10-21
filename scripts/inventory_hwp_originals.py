#!/usr/bin/env python3
"""보관 HWP 원본 인벤토리 + 후보 변환기 대조. **읽기 전용**.

범위: `docs/verification/2026-10-20-companion-app-rollout.md`
「Claude assignment: historical HWP recovery」 1단계.

## 왜 인벤토리가 먼저인가

복구를 논하기 전에 **무엇이 복구 대상인지**가 있어야 한다. 「실패한 HWP 를
되살리자」 는 말에는 세 가지가 섞여 있다.

| 섞여 있는 것 | 사람이 할 일 |
|---|---|
| 원본 바이트가 남아 있다 | 다시 변환해 볼 수 있다 |
| 원본이 없다 | Slack 에서 다시 받는 것이 선행이다 |
| 사람이 올린 것인지 모른다 | **대상이 아니다** — 봇 산출물은 근거가 되지 않는다 |

셋을 한 목록에 두면 두 번째·세 번째가 첫 번째에 묻힌다.

## 사람이 올린 것만 대상이다

봇이 만든 파일은 **사람 원본이 아니다.** rhwp 같은 변환 앱을 채널에 초대하면
그 앱이 PDF 를 채널에 올리고, 그것을 원본으로 세면 **우리 변환본을 우리가 다시
근거로 쓰는 고리**가 생긴다(원칙 1).

수집 경로는 이미 봇 메시지를 버린다(`archiving_bot._ingest`·`backfill`·
`file_recovery._shared_file`). 그래서 **`origin_message_ts` 가 있는 첨부**는 사람
메시지로 들어온 것이다. 그 좌표가 없으면 여기서는 **모른다**로 두고 제외한다 —
파일 단독 복구 경로로 들어온 것은 수집 당시 Slack API 로 업로더를 확인했지만,
이 스크립트는 Slack 을 부르지 않으므로 그 사실을 **여기서 다시 증명할 수 없다.**

## 하지 않는 것

아무것도 쓰지 않는다. metadata·정본·색인·DB 를 건드리지 않고, 외부로 아무것도
보내지 않는다. `--compare` 는 후보 변환기를 **임시 디렉터리에서** 돌릴 뿐이다.

`--dump-dir` 로 내려받은 산출물은 **사내 문서 본문**이다. 사람이 원본과 표·숫자를
맞춰 본 뒤 지우고, 로그·인계 문서·티켓에 내용을 붙이지 않는다(원칙 5).

## 서버에서

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_hwp_originals.py \\
  --archive /var/lib/tybot/archive

sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/inventory_hwp_originals.py \\
  --archive /var/lib/tybot/archive \\
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW --compare
```
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 수집 대상에서 뺀 워크스페이스. 범위 문서가 기본 제외로 못박았다.
EXCLUDED_WORKSPACES = frozenset({"tec"})

HWP_TYPES = ("hwp", "hwpx")

# 제외 사유. **빈 칸으로 두지 않는다** — 빈 칸은 「안 봤다」 와 「안 된다」 를 섞는다.
REASON_WORKSPACE = "excluded-workspace"
REASON_NO_ORIGINAL = "original-not-retained"
REASON_HASH_MISMATCH = "original-hash-mismatch"
REASON_NO_PROVENANCE = "uploader-provenance-unverified"
REASON_BLOCKED = "policy-blocked"


def _meta(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _original(meta: dict) -> tuple[Path | None, int]:
    raw = str(meta.get("object_path") or "")
    if not raw:
        return None, 0
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return None, 0
    return path, path.stat().st_size


def _digest(path: Path) -> str:
    out = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            out.update(chunk)
    return out.hexdigest()


def classify(item, meta: dict) -> tuple[str, dict]:
    """이 첨부가 복구 **대상인가.** 아니면 사유를 돌려준다.

    사유를 코드로 둔다. 문장으로 두면 집계가 안 되고, 집계가 안 되면 「대상이
    아닌 것이 몇 건인지」 를 아무도 모른다.
    """
    detail: dict = {}
    if item.workspace in EXCLUDED_WORKSPACES:
        return REASON_WORKSPACE, detail
    if (meta.get("conversion_state") == "blocked" or meta.get("status") == "pii_refused"
            or meta.get("screen_result") == "blocked"):
        # 정책으로 막힌 자료는 승인으로 우회하지 않는다(원칙 5).
        return REASON_BLOCKED, detail
    path, size = _original(meta)
    if meta.get("original_state") != "retained" or path is None:
        return REASON_NO_ORIGINAL, detail
    detail["actual_bytes"] = size
    stored = str(meta.get("sha256") or "")
    actual = _digest(path)
    detail["sha256_matches"] = bool(stored) and stored == actual
    if not detail["sha256_matches"]:
        # **바이트가 바뀌었거나 해시가 없다.** 둘 다 「검증된 원본」 이 아니다.
        return REASON_HASH_MISMATCH, detail
    if not str(meta.get("origin_message_ts") or "").strip():
        # 사람 메시지로 들어온 좌표가 없다. 파일 단독 복구였을 수도 있지만,
        # 이 스크립트는 Slack 을 부르지 않아 **여기서 증명할 수 없다.**
        return REASON_NO_PROVENANCE, detail
    return "", detail


def inventory(archive: Path, *, file_ids: set[str], workspace: str) -> list[dict]:
    """staging metadata 만 읽는다. Slack·DB 를 부르지 않는다."""
    from tybot import attachment_review

    rows: list[dict] = []
    for item in attachment_review.scan(archive):
        if item.filetype.lower() not in HWP_TYPES:
            continue
        if file_ids and item.file_id not in file_ids:
            continue
        if workspace and item.workspace != workspace:
            continue
        meta = _meta(item.meta_path)
        reason, detail = classify(item, meta)
        rows.append({
            "workspace": item.workspace,
            "channel_id": item.channel_id,
            "file_id": item.file_id,
            "filetype": item.filetype,
            "declared_size": item.size,
            "conversion_state": item.conversion_state,
            "error_code": item.error_code,
            "original_state": item.original_state,
            "staged_at": item.staged_at,
            # 지금 정본이 어느 신원으로 계산돼 있나. 재변환이 그 자리를 덮는지
            # 판단하려면 이 셋이 필요하다.
            "converter_name": str(meta.get("converter_name") or ""),
            "converter_version": str(meta.get("converter_version") or ""),
            "coverage_state": str(meta.get("coverage_state") or ""),
            "eligible": not reason,
            "excluded_reason": reason,
            **detail,
        })
    rows.sort(key=lambda row: (row["workspace"], row["channel_id"], row["file_id"]))
    return rows


def compare(row: dict, archive: Path, *, dump_dir: Path | None) -> dict:
    """후보 변환기들을 돌려 **같은 표에 놓는다.** 읽기 전용."""
    from tybot.archive import hwp_candidates as hc

    meta = {}
    for item in _scan_cache(archive):
        if (item.workspace, item.channel_id, item.file_id) == (
            row["workspace"], row["channel_id"], row["file_id"]
        ):
            meta = _meta(item.meta_path)
            break
    path, _size = _original(meta)
    if path is None:
        return {"skipped": REASON_NO_ORIGINAL}
    data = path.read_bytes()
    out: dict = {"direct": hc.direct_extraction(data, row["filetype"]), "candidates": []}
    for candidate in hc.configured_candidates():
        keep = None
        if dump_dir is not None:
            keep = dump_dir / f"{row['file_id']}.{candidate.name}.pdf"
        result = hc.run_candidate(candidate, data, keep_pdf=keep)
        got = dict(vars(result))
        got["stderr_markers"] = list(result.stderr_markers)
        got["missing_pages"] = list(result.missing_pages)
        # **쪽별 글자 수는 분포만.** 전부 실으면 목록이 그것으로 찬다.
        per = list(result.page_chars)
        got["page_chars"] = {
            "pages": len(per),
            "over_200": sum(1 for n in per if n > 200),
            "under_50": sum(1 for n in per if n <= 50),
            "max": max(per) if per else 0,
        }
        base = out["direct"].get("chars") or 0
        if base and result.chars:
            # **추출량 차이다. 누락률이 아니다** — 어느 쪽이 맞는지 모른다.
            got["extracted_ratio"] = round(result.chars / base, 1)
        out["candidates"].append(got)
    return out


_SCAN: list | None = None


def _scan_cache(archive: Path) -> list:
    global _SCAN
    if _SCAN is None:
        from tybot import attachment_review

        _SCAN = attachment_review.scan(archive)
    return _SCAN


def report(rows: list[dict]) -> None:
    eligible = [r for r in rows if r["eligible"]]
    print(f"HWP 원본 {len(rows)}건 · 복구 대상 {len(eligible)}건")
    print("=" * 72)
    for row in rows:
        mark = "✅" if row["eligible"] else "⛔"
        print(f"{mark} {row['workspace']}/{row['channel_id']}/{row['file_id']} "
              f"({row['filetype']}, {row.get('actual_bytes', row['declared_size'])}B)")
        print(f"   상태={row['conversion_state']} code={row['error_code'] or '-'} "
              f"판={row['converter_name'] or '-'}/{row['converter_version'] or '-'} "
              f"coverage={row['coverage_state'] or '-'}")
        if not row["eligible"]:
            print(f"   제외 사유: {row['excluded_reason']}")
        if row.get("compare"):
            _print_compare(row["compare"])
    counts: dict[str, int] = {}
    for row in rows:
        if not row["eligible"]:
            counts[row["excluded_reason"]] = counts.get(row["excluded_reason"], 0) + 1
    if counts:
        print("=" * 72)
        print("제외 사유별:")
        for reason, count in sorted(counts.items()):
            print(f"  {reason}: {count}건")
    print("=" * 72)
    print("**아무것도 고치지 않았습니다** — 조회 전용입니다.")


def _print_compare(data: dict) -> None:
    if data.get("skipped"):
        print(f"   대조 건너뜀: {data['skipped']}")
        return
    direct = data.get("direct") or {}
    if direct.get("failed"):
        print(f"   지금 경로: 실패({direct['failed']})")
    else:
        print(f"   지금 경로: {direct.get('chars')}자 · 표 {direct.get('table_rows')}행 "
              f"· coverage {direct.get('coverage_state')} "
              f"{direct.get('coverage_converted')}/{direct.get('coverage_total')}")
    for got in data.get("candidates") or []:
        name = got["name"]
        if got.get("skipped"):
            print(f"   [{name}] 건너뜀: {got['skipped']}")
            continue
        head = (f"   [{name}] exit={got.get('exit_code')} "
                f"{got.get('seconds')}초 · 쪽={got.get('pages')}")
        if got.get("signal_name"):
            head += f" · signal={got['signal_name']}"
        print(head)
        if got.get("error"):
            print(f"      오류: {got['error']}")
        markers = got.get("stderr_markers") or []
        if markers or got.get("stderr_lines"):
            print(f"      stderr {got.get('stderr_lines')}줄 · "
                  f"표식={', '.join(markers) or '걸린 것 없음'}")
        if got.get("chars") is not None:
            print(f"      {got['chars']}자 · 표 {got['table_rows']}행 · "
                  f"coverage {got['coverage_state']} "
                  f"{got['coverage_converted']}/{got['coverage_total']}")
            if got.get("missing_pages"):
                print(f"      미확인 쪽: {got['missing_pages']}")
            dist = got.get("page_chars") or {}
            print(f"      쪽별 200자 초과 {dist.get('over_200')} · "
                  f"50자 이하 {dist.get('under_50')} · 최대 {dist.get('max')}")
        if got.get("extracted_ratio"):
            print(f"      ⇒ 추출량 차이: 지금 경로의 {got['extracted_ratio']}배")
            print("         **누락률이 아니다.** 어느 쪽이 맞는지는 사람이 원본과 맞춰야 한다")
        if got.get("pdf_sha256"):
            print(f"      PDF sha256={got['pdf_sha256'][:16]} · {got.get('pdf_bytes')}B")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="보관 HWP 원본 인벤토리 (조회 전용)")
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--file-id", action="append", default=[])
    parser.add_argument("--compare", action="store_true",
                        help="후보 변환기를 돌려 지금 경로와 추출량을 대조한다")
    parser.add_argument("--dump-dir", default="",
                        help="후보가 만든 PDF 를 저장한다. **사내 문서 본문이라** "
                             "원본 대조 후 지우고 로그·문서에 붙이지 않는다")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        from tybot.envfile import load_env_file

        load_env_file()
    except Exception:  # noqa: BLE001 - 설정이 없어도 아카이브는 볼 수 있다
        pass

    archive = Path(args.archive)
    if not archive.is_dir():
        print(f"아카이브 디렉터리가 없습니다: {archive}", file=sys.stderr)
        return 2
    if args.dump_dir and not args.compare:
        print("--dump-dir 는 --compare 와 함께 씁니다.", file=sys.stderr)
        return 2
    if args.workspace in EXCLUDED_WORKSPACES:
        print(f"{args.workspace} 는 이 경로의 대상이 아닙니다.", file=sys.stderr)
        return 2

    rows = inventory(archive, file_ids={f for f in args.file_id if f},
                     workspace=args.workspace)
    if not rows:
        print("대상을 찾지 못했습니다.", file=sys.stderr)
        return 1
    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if args.compare:
        for row in rows:
            if row["eligible"]:
                row["compare"] = compare(row, archive, dump_dir=dump_dir)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        report(rows)
    if dump_dir is not None:
        print(f"\n저장 위치: {dump_dir}")
        print("**사내 문서 본문입니다.** 원본과 표·숫자를 맞춘 뒤 지우고, "
              "로그·인계 문서·티켓에 내용을 붙이지 마십시오.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
