"""채널 요약 검토자 (B-37).

요약은 봇이 **후보만** 만들고 사람이 확정한다. 그 사람을 정하고 찾는 자리다.
설계: [`docs/design/summary-review.md`](../../docs/design/summary-review.md)

## 지키는 것 셋

1. **검토자가 없으면 요약을 반영하지 않는다.** "없으면 자동 반영" 이 아니다.
   대신 **없다는 사실을 밖으로 낸다**(`channels_without_reviewer`) — 조용히
   아무것도 안 하는 것이 우리가 가장 자주 겪은 고장이다.
2. **채널 ID 로 저장한다.** 이름으로 두면 `/채널 이름변경` 이 검토자를 조용히
   지운다. 이름은 표시용으로만 함께 둔다.
3. **삭제 대신 사용 중지.** 지우면 언제부터 검토가 멈췄는지 알 수 없다.

## DB 인 이유

채널 소유자(`ChannelOwnerStore`)는 JSON 파일이다. 그건 "누가 만들었나" 하나뿐이라
참조하는 것이 없다. 검토자는 다르다 — 후보 목록·발송 이력·헬스 체크가 함께 읽고,
봇·타이머·콘솔 세 프로세스가 동시에 본다. 파일 잠금으로 버티는 자리가 아니다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import time

log = logging.getLogger("tybot.reviewers")

DEFAULT_SEND_AT = time(8, 0)


class ReviewerError(Exception):
    """검토자를 읽거나 쓸 수 없다. 호출부가 사용자에게 그대로 보여도 되는 문구다."""


@dataclass(frozen=True)
class Reviewer:
    workspace: str
    channel_id: str
    channel_name: str
    reviewer_user: str
    send_at: time
    enabled: bool


def parse_send_at(raw: str) -> time:
    """`08:00`·`8`·`0800` 을 받는다.

    사람이 입력하는 값이라 모양이 여러 가지다. 못 읽으면 **기본값으로 넘어가지 않고
    거절한다** — 09:00 로 적었는데 08:00 에 오면 설정이 안 된 것으로 보인다.
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_SEND_AT
    digits = text.replace(":", "").replace("시", "")
    try:
        if len(digits) <= 2:
            hour, minute = int(digits), 0
        else:
            hour, minute = int(digits[:-2]), int(digits[-2:])
    except ValueError as exc:
        raise ReviewerError(f"보낼 시각을 읽지 못했습니다: {raw!r} (예: 08:00)") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ReviewerError(f"보낼 시각이 범위를 벗어났습니다: {raw!r}")
    return time(hour, minute)


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise ReviewerError("DATABASE_URL 이 없어 검토자를 저장할 수 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except ReviewerError:
        raise
    except Exception as exc:
        raise ReviewerError(f"검토자 DB 연결 실패: {exc}") from exc


def _row(row: dict) -> Reviewer:
    return Reviewer(
        workspace=str(row["workspace"]),
        channel_id=str(row["channel_id"]),
        channel_name=str(row.get("channel_name") or ""),
        reviewer_user=str(row["reviewer_user"]),
        send_at=row["send_at"],
        enabled=bool(row["enabled"]),
    )


def set_reviewers(
    *,
    workspace: str,
    channel_id: str,
    channel_name: str,
    reviewer_users: list[str],
    send_at: time,
    set_by: str,
) -> list[Reviewer]:
    """이 채널의 검토자를 **주어진 목록으로 맞춘다.**

    목록에서 빠진 사람은 지우지 않고 **끈다.** 다시 넣으면 되살아난다.
    빈 목록이면 전부 끈다 — 그러면 이 채널은 요약을 반영하지 않는 상태가 된다.
    """
    if not channel_id:
        raise ReviewerError("채널 ID 가 없습니다.")
    users = [u.strip() for u in reviewer_users if u and u.strip()]
    if len(set(users)) != len(users):
        raise ReviewerError("같은 사람이 두 번 들어 있습니다.")

    try:
        with _connect() as conn, conn.cursor() as cur:
            # 먼저 전부 끈다. 그 다음 주어진 사람만 켠다 — 두 문장을 한 트랜잭션에
            # 두어야 "아무도 검토자가 아닌" 순간이 밖에서 보이지 않는다.
            cur.execute(
                """
                UPDATE channel_reviewer SET enabled = false
                 WHERE workspace = %(ws)s AND channel_id = %(ch)s
                """,
                {"ws": workspace, "ch": channel_id},
            )
            for user in users:
                cur.execute(
                    """
                    INSERT INTO channel_reviewer
                           (workspace, channel_id, channel_name, reviewer_user,
                            send_at, enabled, set_by)
                    VALUES (%(ws)s, %(ch)s, %(name)s, %(user)s, %(at)s, true, %(by)s)
                    ON CONFLICT (workspace, channel_id, reviewer_user) DO UPDATE
                       SET channel_name = excluded.channel_name,
                           send_at      = excluded.send_at,
                           enabled      = true,
                           set_by       = excluded.set_by,
                           set_at       = now()
                    """,
                    {
                        "ws": workspace,
                        "ch": channel_id,
                        "name": channel_name,
                        "user": user,
                        "at": send_at,
                        "by": set_by,
                    },
                )
            cur.execute(
                """
                SELECT * FROM channel_reviewer
                 WHERE workspace = %(ws)s AND channel_id = %(ch)s AND enabled
                 ORDER BY reviewer_user
                """,
                {"ws": workspace, "ch": channel_id},
            )
            rows = [_row(r) for r in cur.fetchall()]
            conn.commit()
    except ReviewerError:
        raise
    except Exception as exc:
        raise ReviewerError(f"검토자 저장 실패: {exc}") from exc

    log.info(
        "검토자 설정 ws=%s ch=%s 인원=%d 시각=%s by=%s",
        workspace,
        channel_id,
        len(rows),
        send_at,
        set_by,
    )
    return rows


def reviewers_for(workspace: str, channel_id: str) -> list[Reviewer]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM channel_reviewer
                 WHERE workspace = %s AND channel_id = %s AND enabled
                 ORDER BY reviewer_user
                """,
                (workspace, channel_id),
            )
            return [_row(r) for r in cur.fetchall()]
    except ReviewerError:
        raise
    except Exception as exc:
        raise ReviewerError(f"검토자 조회 실패: {exc}") from exc


def has_reviewer(workspace: str, channel_id: str) -> bool:
    """요약을 반영해도 되는가. **읽기 실패는 '없음' 으로 본다.**

    막는 쪽이 기본값이다 — DB 를 못 읽었을 때 반영해 버리면, 장애가 곧 무단 반영이 된다.
    """
    try:
        return bool(reviewers_for(workspace, channel_id))
    except ReviewerError as exc:
        log.warning("검토자를 확인하지 못해 반영하지 않습니다: %s", exc)
        return False


def channels_without_reviewer(workspace_channels: dict[str, list[tuple[str, str]]]) -> list[dict]:
    """검토자가 없는 채널 목록. 헬스 체크가 이것을 화면에 올린다.

    인자는 `{워크스페이스: [(채널ID, 채널명), ...]}` — 채널 목록은 Slack 이 알고
    이 모듈은 모른다. 여기서 Slack 을 부르면 검토자 조회가 Slack 장애에 묶인다.
    """
    if not workspace_channels:
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT workspace, channel_id FROM channel_reviewer WHERE enabled"
            )
            covered = {(str(r["workspace"]), str(r["channel_id"])) for r in cur.fetchall()}
    except ReviewerError as exc:
        log.warning("검토자 목록을 읽지 못했습니다: %s", exc)
        return []

    out = []
    for workspace, channels in workspace_channels.items():
        for channel_id, channel_name in channels:
            if (workspace, channel_id) not in covered:
                out.append({
                    "workspace": workspace,
                    "channelId": channel_id,
                    "channelName": channel_name,
                })
    return out
