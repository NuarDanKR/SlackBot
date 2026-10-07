"""채널 공개 여부 manifest — **권위는 `archive_channel_mode.is_private` 다.**

사용(서버):
    sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.archive.privacy_manifest \\
        --workspace tyit --out /var/lib/tybot/state/privacy-manifest.json

## 무엇을 푸는가

Hermes 가 `pf-archiver` 로 전환할 때, 어느 채널이 비공개인지 알아야 한다. 그런데
정본(`archive/raw/*.md`)의 `visibility` 에는 그 정보가 없다 — 수집기가 그 값을 안
넘겨서 전 채널이 writer 기본값 `private` 로 박힌다. 첨부 정본에만 실제 값이 실리는데,
첨부 없는 채널에는 아무 신호가 없다.

진짜 권위는 **Slack 의 `conversations.list` 가 준 `is_private`** 이고, 그 값은
`channel_membership.sync` 가 `archive_channel_mode` 에 적어 둔다. 그 표를 그대로
내보낸다.

## Hermes 는 DB 에 안 붙는다

Hermes 는 **파일만 읽는다.** DB 자격증명을 Hermes 쪽에 두면 그 순간 PF 에
운영 DB 로 가는 길이 하나 더 생기고, 그 길은 읽기 전용이라는 보장이 없다. 내보내는
쪽이 TYBot 이고 받는 쪽은 JSON 한 장이다.

## 담는 것이 다섯뿐인 이유

`workspace` · `channel_id` · `channel_name` · `is_private` · 생성 시각. 끝이다.

이 파일은 **조직 경계를 넘어 다닌다**(사내 → PF). 원문·토큰·DSN 은 물론이고
`note`·`updated_by`·`cutover_ts` 같은 운영 흔적도 담지 않는다 — 공개 여부를 판정하는
데 필요 없고, 필요 없는 것을 담으면 그 파일이 언젠가 다른 용도로 쓰인다.

## 확인된 행만 내보낸다

`is_private` 의 기본값은 `false` 다. 즉 **한 번도 동기화되지 않은 행은 「공개」 라고
적혀 있다.** 그 값을 그대로 내보내면 비공개 채널이 공개로 선언되고, 그건 조용히
새는 쪽이다.

그래서 `membership_checked_at IS NOT NULL` 인 행만 담는다 — 그 칸은 `is_private` 와
**같은 UPDATE 문**에서 채워지므로(`archiver_save_membership`), 값이 있다는 것은
Slack 에서 실제로 받아 적었다는 뜻이다. 빠진 채널은 받는 쪽에서 「미확인」 이 되어
전환을 막는다. 묻는 쪽으로 틀린다.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 형식 이름. 받는 쪽(`subbots/hermes/scripts/check-archiver-privacy.js`)이 이 값을
#: 확인하고, 다르면 **「공개 여부를 모른다」** 로 다룬다 — 짐작해 읽지 않는다.
SCHEMA = "channel-privacy-manifest/v1"

#: 한 채널 행에 담는 키. **여기 없는 것은 안 담는다.**
ROW_KEYS = ("channel_id", "channel_name", "is_private")

#: 확인된 행만 고른다. 읽기 전용 — 이 모듈은 `SELECT` 말고는 하지 않는다.
QUERY = """
SELECT channel_id, channel_name, is_private
  FROM archive_channel_mode
 WHERE workspace = %s
   AND membership_checked_at IS NOT NULL
 ORDER BY channel_id
"""


@dataclass(frozen=True)
class Channel:
    channel_id: str
    channel_name: str
    is_private: bool


def fetch(conn, workspace: str) -> list[Channel]:
    """확인된 채널 행. **읽기만 한다.**

    트랜잭션을 읽기 전용으로 고정한다 — 이 모듈이 쓰기를 할 리 없지만, 「할 리
    없다」 와 「할 수 없다」 는 다르다. 운영 DB 에 붙는 코드라 후자로 둔다.
    지원하지 않는 드라이버·연결이면 조용히 넘어간다(판정이 아니라 보호막이다).
    """
    with conn.cursor() as cur:
        # 못 거는 드라이버·연결에서는 조용히 넘어간다 — 보호막이지 전제가 아니다.
        with contextlib.suppress(Exception):
            cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(QUERY, (workspace,))
        rows = cur.fetchall()
    out: list[Channel] = []
    for row in rows:
        get = row.get if isinstance(row, dict) else None
        channel_id = str((get("channel_id") if get else row[0]) or "")
        if not channel_id:
            continue
        out.append(Channel(
            channel_id=channel_id,
            channel_name=str((get("channel_name") if get else row[1]) or ""),
            is_private=bool(get("is_private") if get else row[2]),
        ))
    return out


def build(workspace: str, channels: list[Channel], *, now: datetime | None = None) -> dict:
    """내보낼 덩이. **키를 여기서 고정한다.**

    행을 dict 로 받아 그대로 싣지 않는다 — 그러면 표에 열이 늘어나는 날 그 열이
    조용히 PF 로 건너간다.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    return {
        "schema": SCHEMA,
        "workspace": workspace,
        "generated_at": stamp,
        "channels": [
            {"channel_id": c.channel_id, "channel_name": c.channel_name,
             "is_private": bool(c.is_private)}
            for c in channels
        ],
    }


def write(path: Path | str, payload: dict) -> int:
    """원자적으로 쓴다. 받는 쪽은 **항상 온전한 한 벌**을 본다.

    반쯤 쓰인 JSON 은 받는 쪽에서 「못 읽음」 이 되고, 그건 fail-closed 라 전환이
    막힌다 — 안전하지만 사유가 「manifest 가 깨졌다」 로만 보여 원인을 못 찾는다.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=file.parent, prefix=f".{file.name}.", suffix=".tmp")
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(file)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(payload.get("channels") or [])


def export(conn, workspace: str, path: Path | str, *, now: datetime | None = None) -> int:
    return write(path, build(workspace, fetch(conn, workspace), now=now))


def _connect():
    import psycopg

    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="채널 공개 여부 manifest 를 내보낸다 (읽기 전용)",
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--out", required=True, help="내보낼 JSON 경로")
    args = parser.parse_args(argv)

    try:
        with _connect() as conn:
            count = export(conn, args.workspace, args.out)
    except KeyError:
        # **DSN 을 화면에 적지 않는다.** 오류 문구는 로그로 복사되고, 복사된 로그는
        # 저장소·메신저로 건너간다.
        print("DATABASE_URL 이 설정되지 않았습니다.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"manifest 를 내보내지 못했습니다: {type(exc).__name__}", file=sys.stderr)
        return 2

    print(f"확인된 채널 {count}개를 {args.out} 로 내보냈습니다.")
    print("아직 멤버십 동기화를 안 거친 채널은 빠집니다 — 받는 쪽에서 「미확인」 으로 "
          "전환을 막습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
