"""봇 분류 상태의 「답변 봇」 을 감사기록으로 바로잡는다 (1회성 보정).

## 왜 필요한가
`specialist_call.bot` 열이 생길 때(2026-11-18), 그 전 기록 중 「11-12 16시 이후 Hermes 를
Opus 로 부른 호출」 을 VIPBot 으로 채웠다. VIPBot 만 Opus 를 쓴다는 가정이었는데, TYBot 에서도
`--model=claude-opus-…` 로 모델을 지정해 물으면 Opus 가 쓰인다. 그래서 TYBot 질문 일부가
VIPBot 으로 잘못 표시됐다(예: 11-17·11-18 토목 이해동 DM).

## 무엇으로 가리나
어느 봇이 받았는지는 **감사기록이 정답**이다. TYBot 과 VIPBot 은 감사기록 폴더가 다르다.

| 폴더 | 봇 |
|---|---|
| `QA_LOG_DIR` | TYBot |
| `VIPBOT_QA_LOG_DIR` (기본 `/var/lib/tybot-vipbot/qa`) | VIPBot |

호출의 `qa_record_id` 가 어느 폴더의 `record_id` 에 있는지 보고 `bot` 을 고친다. 어느 쪽에도
없으면 건드리지 않는다(모르는 것을 추측으로 채우지 않는다).

## 쓰는 법 (서버)
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
      /opt/tybot/.venv/bin/python /opt/tybot/scripts/fix_specialist_call_bot.py          # 미리 보기
    sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
      /opt/tybot/.venv/bin/python /opt/tybot/scripts/fix_specialist_call_bot.py --apply  # 고치기

질문·답변 본문은 읽지도 출력하지도 않는다 — `record_id` 만 모은다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VIP_QA_DEFAULT = "/var/lib/tybot-vipbot/qa"


def record_ids(directory: Path) -> set[str]:
    """그 폴더 감사기록(`qa-YYYY-MM.jsonl`)의 record_id 전부. 본문은 보지 않는다."""
    ids: set[str] = set()
    if not directory.is_dir():
        return ids
    for path in sorted(directory.glob("qa-*.jsonl")):
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rid = str(json.loads(line).get("record_id") or "")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if rid:
                        ids.add(rid)
        except OSError as exc:
            print(f"! 읽지 못함: {path} ({type(exc).__name__})", file=sys.stderr)
    return ids


def plan(rows: list[dict], tybot_ids: set[str], vip_ids: set[str]) -> list[tuple[int, str, str]]:
    """(id, 지금 bot, 바꿀 bot). 감사기록으로 확인되는 것만."""
    changes: list[tuple[int, str, str]] = []
    for row in rows:
        rid = str(row.get("qa_record_id") or "")
        if not rid:
            continue
        in_ty, in_vip = rid in tybot_ids, rid in vip_ids
        if in_ty == in_vip:          # 둘 다 없거나(모름) 둘 다 있으면(이상) 두지 않는다
            continue
        want = "tybot" if in_ty else "vipbot"
        if (row.get("bot") or "") != want:
            changes.append((int(row["id"]), str(row.get("bot") or ""), want))
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="실제로 고친다(없으면 미리 보기)")
    args = parser.parse_args(argv)

    from tybot.envfile import load_env_file

    load_env_file()   # QA_LOG_DIR·DATABASE_URL 은 /etc/tybot/tybot.env 에 있다
    from tybot.console.reader import qa_log_dir
    from tybot.console.workspace_store import _connect

    tybot_ids = record_ids(qa_log_dir())
    vip_ids = record_ids(Path(os.getenv("VIPBOT_QA_LOG_DIR", VIP_QA_DEFAULT)))
    print(f"감사기록 record_id: TYBot {len(tybot_ids)}건 · VIPBot {len(vip_ids)}건")
    if not tybot_ids:
        print("TYBot 감사기록을 찾지 못했습니다 — QA_LOG_DIR 을 확인하세요. 아무것도 바꾸지 않습니다.")
        return 2

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, at, workspace, bot, qa_record_id FROM specialist_call
                        WHERE qa_record_id <> '' ORDER BY at""")
        rows = [dict(r) for r in cur.fetchall()]
        changes = plan(rows, tybot_ids, vip_ids)
        by_id = {int(r["id"]): r for r in rows}
        print(f"고칠 호출 {len(changes)}건")
        for cid, before, after in changes:
            r = by_id[cid]
            print(f"  #{cid} {r['at']:%m-%d %H:%M} {r['workspace']}: {before or '(빈 값)'} → {after}")
        if not args.apply:
            print("미리 보기입니다. 고치려면 --apply 를 붙여 다시 실행하세요.")
            return 0
        for cid, _before, after in changes:
            cur.execute("UPDATE specialist_call SET bot = %s WHERE id = %s", (after, cid))
    print(f"고쳤습니다: {len(changes)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
