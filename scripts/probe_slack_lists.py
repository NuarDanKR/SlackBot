#!/usr/bin/env python
"""Slack Lists 행 첨부 **읽기 전용 실측** (B-90 1단계).

계약: `docs/verification/2026-11-10-slack-lists-attachments.md`

## 무엇을 하지 않나

이 스크립트는 **읽기만** 한다. 파일을 내려받지 않고, 아카이브·DB 에 쓰지 않고,
Slack 에 아무것도 만들거나 고치지 않는다. 쓰기 메서드 이름은 이 파일에 없다.

## 무엇을 출력하지 않나

응답 본문·행 텍스트·파일명·사용자 정보를 **출력하지도 저장하지도 않는다.**
남기는 것은 구조(어떤 칸이 있나)와 건수, 그리고 비식별 fixture 다.

실측 대상이 운영 채널이라 이 규칙이 전부다. 「구조만 보려고」 켠 스크립트가
사람 이름을 터미널에 흘리면, 그 터미널 기록이 곧 유출이다.

ID 도 그대로 남기지 않는다. `files.info` 의 가시성을 확인하려면 ID 가 필요하지만
**기록에는** 짧은 해시로 적는다 — 이 저장소는 공개 저장소가 아니지만, 검증
문서는 사람이 복사해 옮긴다.

## 쓰는 법

```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/probe_slack_lists.py \
    --workspace tyit --channel C0ABCDEF123
```

`--channel` 은 **콘솔에 등록된 관리 채널 하나**여야 한다. 워크스페이스 전체를
훑는 선택지는 없다(계약 §범위 밖).

토큰은 **콘솔 DB** 에서 온다(`bot_connection` · `console/bot_identity`).
환경변수를 보지 않는다 — 이 저장소에서 Slack 토큰의 정본은 DB 이고, env 로 따로
읽으면 콘솔에서 토큰을 갈아 끼운 날 이 스크립트만 옛 토큰으로 돌아
「그 봇이 못 본다」 는 **틀린 결론**이 나온다. `DATABASE_URL` 만
`/etc/tybot/tybot.env` 에서 읽는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field

#: 한 쪽에 읽을 행 수. Slack 권장 범위 안에서 작게 둔다 — 실측은 양이 아니라
#: 모양을 보는 일이고, 큰 요청은 rate limit 을 더 빨리 만난다.
PAGE_SIZE = 100

#: 몇 쪽까지 따라갈까. 끝까지 못 읽으면 **그 List 전체를 이번 회차에 쓰지
#: 않는다**(계약 §닫힌 수집 절차 2). 반쪽 목록은 「첨부 없음」 으로 읽힌다.
MAX_PAGES = 50

#: 행 필드에서 파일 좌표로 **인정하는** 모양 둘. 행 텍스트와 일반 URL 은 보지
#: 않는다 — 정규식으로 추측한 좌표는 틀렸을 때 조용하다.
ATTACHMENT_KEY = "attachment"
LINK_KEY = "link"

#: 이 이름이 코드에 나오면 안 된다. `lists:write` 가 없어도 호출은 **런타임에**
#: 거절되고, 그때는 이미 요청이 나간 뒤다.
FORBIDDEN_METHODS = (
    "slackLists_create",
    "slackLists_update",
    "slackLists_items_create",
    "slackLists_items_update",
    "slackLists_items_delete",
    "slackLists_items_deleteMultiple",
    "slackLists_access_set",
    "slackLists_access_delete",
)


#: 기본으로 읽을 봇. **Lists 수집은 Archiver 의 일이다**(B-90 권한 계약) —
#: 답변 런타임(TYBot·VIPBot)에 수집 책임을 더하지 않는다. 실측도 같은 토큰으로
#: 해야 「이 봇이 볼 수 있나」 가 실제 답이 된다.
ARCHIVER_BOT = "archiver"


def bot_token(workspace: str, bot_key: str) -> str:
    """콘솔 DB(`bot_connection`)에서 봇 토큰 하나.

    **환경변수를 보지 않는다.** 이 저장소에서 Slack 토큰의 정본은 DB 다
    (`console/bot_identity`). env 로 따로 읽으면 콘솔에서 토큰을 갈아 끼운 날
    이 스크립트만 옛 토큰으로 돌고, 그 결과를 「그 봇이 못 본다」 로 읽게 된다.

    `runtime_tokens` 는 **enabled + 신원 확인 통과**만 돌려준다. 꺼 둔 연결의
    토큰으로 실측하면 운영에서 실제로 쓰이지 않을 권한을 보게 된다.
    """
    from tybot.console.bot_identity import runtime_tokens

    token, _app_token = runtime_tokens(workspace, bot_key)
    return token


def _as_list(value: object) -> list:
    """목록만 목록으로 본다. 문자열은 **목록이 아니다.**

    `isinstance(value, Iterable)` 로 보면 문자열이 통과하고, 그 순간 글자가
    원소가 된다. 이 구분을 빠뜨리는 것이 파이썬에서 가장 흔한 조용한 버그다.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def short(value: object) -> str:
    """ID 를 짧은 해시로. 같은 ID 는 같은 값이 되어 대조는 되고 복원은 안 된다."""
    raw = str(value or "")
    if not raw:
        return "-"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


@dataclass
class ListProbe:
    """List 하나의 실측. **본문은 담지 않는다.**"""

    list_ref: str
    pages: int = 0
    rows: int = 0
    complete: bool = False
    failure: str = ""
    #: 행에 나온 칸의 종류와 횟수. 칸 **이름**은 설정에 따라 사람이 쓴 말일 수
    #: 있어(예: 「담당자」) 해시로 적는다.
    column_kinds: Counter = field(default_factory=Counter)
    attachment_files: set[str] = field(default_factory=set)
    link_files: set[str] = field(default_factory=set)
    #: 같은 파일이 여러 행·여러 칸에 붙은 횟수.
    duplicate_hits: int = 0

    def row_fields(self, row: dict) -> None:
        for field_row in row.get("fields") or []:
            if not isinstance(field_row, dict):
                continue
            kind = str(field_row.get("key") or field_row.get("column_id") or "")
            self.column_kinds[short(kind)] += 1
            # **목록인지 먼저 본다.** 문자열이 오면 `for` 가 글자 하나하나를
            # 돌려주고, 그 글자가 전부 파일 ID 가 된다 — 그 다음 `files.info`
            # 호출이 쓰레기 ID 로 나가고, 응답은 「없는 파일」 이다. Slack 응답
            # 모양이 바뀌는 날 조용히 그렇게 된다.
            for file_id in _as_list(field_row.get(ATTACHMENT_KEY)):
                if isinstance(file_id, str) and file_id:
                    if file_id in self.attachment_files:
                        self.duplicate_hits += 1
                    self.attachment_files.add(file_id)
            for link in _as_list(field_row.get(LINK_KEY)):
                if not isinstance(link, dict):
                    continue
                file_id = str(((link.get("file") or {}).get("file_id")) or "")
                if file_id:
                    if file_id in self.link_files:
                        self.duplicate_hits += 1
                    self.link_files.add(file_id)

    def report(self) -> dict:
        return {
            "list": self.list_ref,
            "pages": self.pages,
            "rows": self.rows,
            "complete": self.complete,
            "failure": self.failure,
            "column_kinds": len(self.column_kinds),
            "attachment_file_count": len(self.attachment_files),
            "link_file_count": len(self.link_files),
            "duplicate_hits": self.duplicate_hits,
        }


def find_lists(client, channel: str) -> tuple[list[dict], str]:
    """등록 채널 하나에서 `filetype=list` 만. 워크스페이스를 훑지 않는다."""
    found: list[dict] = []
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            res = client.files_list(
                channel=channel, types="list", limit=PAGE_SIZE, cursor=cursor
            ) or {}
        except Exception as exc:  # noqa: BLE001 - 사유만 남긴다
            return [], _reason(exc)
        for item in res.get("files") or []:
            if isinstance(item, dict) and str(item.get("filetype")) == "list":
                found.append(item)
        cursor = str(((res.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            return found, ""
    return found, "too_many_pages"


def _reason(exc: Exception) -> str:
    data = getattr(getattr(exc, "response", None), "data", {}) or {}
    return str(data.get("error") or type(exc).__name__)


def probe_list(client, list_id: str) -> ListProbe:
    """한 List 의 모든 쪽. **한 쪽이라도 실패하면 미완으로 남긴다.**

    일부 행만 읽고 완료로 표시하면, 그 뒤 행에 붙은 첨부가 「없는 것」 이 된다.
    없는 것과 못 읽은 것을 같게 다루는 순간 이 조사의 의미가 사라진다.
    """
    probe = ListProbe(list_ref=short(list_id))
    cursor = ""
    for _ in range(MAX_PAGES):
        try:
            res = client.slackLists_items_list(
                list_id=list_id, limit=PAGE_SIZE, cursor=cursor
            ) or {}
        except Exception as exc:  # noqa: BLE001
            probe.failure = _reason(exc)
            return probe
        probe.pages += 1
        for row in res.get("items") or []:
            if isinstance(row, dict):
                probe.rows += 1
                probe.row_fields(row)
        cursor = str(((res.get("response_metadata") or {}).get("next_cursor")) or "")
        if not cursor:
            probe.complete = True
            return probe
    probe.failure = "too_many_pages"
    return probe


def check_visibility(client, channel: str, file_ids: set[str]) -> dict:
    """행 첨부가 **그 관리 채널에서 보이는지**.

    보이지 않으면 내려받지 않는다. 계약은 여기서 멈추라고 말한다 — List 접근권과
    파일 접근권을 결합하는 공식 계약을 추측으로 메우지 않는다.
    """
    same, other, unknown = 0, 0, 0
    for file_id in sorted(file_ids):
        try:
            info = (client.files_info(file=file_id) or {}).get("file") or {}
        except Exception:  # noqa: BLE001 - 가시성 미확인은 거절이지 통과가 아니다
            unknown += 1
            continue
        shared = set(info.get("channels") or []) | set(info.get("groups") or [])
        if channel in shared:
            same += 1
        elif shared:
            other += 1
        else:
            unknown += 1
    return {"same_channel": same, "other_channel_only": other, "unverified": unknown}


def probe_channel(client, workspace: str, channel: str) -> dict:
    """등록 채널 하나의 실측 결과. **Slack 읽기 외에는 아무것도 하지 않는다.**

    `main` 에서 떼어 둔 이유는 시험 때문만이 아니다. 인자 파싱·토큰 읽기와
    섞여 있으면 「이 함수가 쓰는 일을 하나」 를 읽어서 확인할 수 없다.
    """
    lists, failure = find_lists(client, channel)
    out: dict = {
        "workspace": short(workspace),
        "channel": short(channel),
        "list_count": len(lists),
        "list_lookup_failure": failure,
        "lists": [],
    }
    if failure:
        return out

    for item in lists:
        list_id = str(item.get("id") or "")
        if not list_id:
            continue
        probe = probe_list(client, list_id)
        row = probe.report()
        if probe.complete:
            row["visibility"] = check_visibility(
                client, channel, probe.attachment_files | probe.link_files
            )
        else:
            # 미완인 List 는 가시성도 묻지 않는다. 일부 파일만 확인하면
            # 「확인했다」 는 기록이 반쪽 사실이 된다.
            row["visibility"] = None
        out["lists"].append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Slack Lists 행 첨부 읽기 전용 실측")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--channel", required=True, help="콘솔에 등록된 관리 채널 하나")
    ap.add_argument(
        "--bot", default=ARCHIVER_BOT,
        help=f"어느 봇의 토큰으로 읽을까 (기본 {ARCHIVER_BOT})",
    )
    ap.add_argument(
        "--fixture", default="", help="비식별 fixture 를 쓸 경로(선택)"
    )
    args = ap.parse_args()

    from tybot.envfile import load_env_file

    load_env_file()
    try:
        token = bot_token(args.workspace, args.bot)
    except Exception as exc:  # noqa: BLE001 - 사유만 보이고 값은 보이지 않는다
        print(f"봇 토큰을 가져오지 못했습니다: {type(exc).__name__}")
        print(
            "콘솔 > 봇 연결에서 "
            f"{args.workspace}/{args.bot} 이 enabled 이고 신원 확인을 통과했는지 "
            "확인하세요. DATABASE_URL 은 /etc/tybot/tybot.env 에 있습니다."
        )
        return 2

    from slack_sdk import WebClient

    client = WebClient(token=token)

    out = probe_channel(client, args.workspace, args.channel)
    if out["list_lookup_failure"]:
        # **「없음」 이 아니다.** 목록을 못 읽은 것과 List 가 없는 것은 다르다.
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.fixture:
        from pathlib import Path

        Path(args.fixture).write_text(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"fixture: {args.fixture}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
