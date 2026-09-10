"""검토자에게 하루치를 밀어 준다 — 사람이 찾아오게 하지 않는다.

설계: [`docs/design/summary-review.md`](../../docs/design/summary-review.md) 3단계

## 왜 밀어 주는가 (2026-09-08, 오너 결정)

첨부 승인을 `/첨부` 로만 열어 뒀다. 결과는 **대기 31건·승인 0건**이었다.

> "사용자는 이 첨부 파일이 승인이 필요한지도 모를거고 실패한지도 모를거야."

맞다. 사람이 **모르는 일을 하러 찾아오지는 않는다.** 당겨 가는 방식은 게이트가
아니라 정체였다. 그래서 두 가지를 바꿨다.

1. 변환된 첨부는 아예 사람을 기다리지 않는다(`attachment_review.find_sendable`).
   추출 텍스트가 아카이브에 들어갔다는 것이 곧 수집 단계 PII 검사를 통과했다는 뜻이다.
2. 그래도 남는 것 — **텍스트가 없어 PII 검사가 돌지 않는 스캔본·이미지** — 은
   채널 검토자에게 **정해진 시각에 하루치로 밀어 준다.** 여기다.

`/첨부` 는 남긴다. 밀어 준 것을 놓쳤을 때 다시 볼 자리는 있어야 한다.

## 지키는 것 다섯

**막힌 것만 담는다.** 이미 답변에 쓰이고 있는 파일까지 목록에 넣으면, 사람이
목록을 「할 일 없음」 으로 읽고 그 다음부터 열지 않는다.

**오늘 것과 밀린 것을 섞지 않는다.** 밀린 31건 사이에 오늘 올라온 2건을 끼워 넣으면
오늘 것이 묻힌다. 오늘 것을 건별로 보이고, 밀린 것은 **건수 한 줄**로만 말한다.

**하루에 한 번.** 멱등 키는 `(워크스페이스, 채널, 받는 사람, 날짜, 종류)` 다.
타이머가 1분마다 돌아도 두 번 가지 않는다.

**검토자가 없으면 채널 개설자에게.** 둘 다 없으면 **보내지 않고 그 사실을 남긴다** —
조용히 아무 일도 안 일어나는 것이 우리가 가장 자주 겪은 고장이다.

**보낼 것이 없으면 보내지 않는다.** 매일 "없습니다" 가 오면 사람이 그 DM 을 끈다.

## 종류를 나눠 둔 이유

`kind` 로 첨부(`attachment`)와 요약 후보(`summary`)를 가른다. 둘은 **같은 사람에게
같은 시각에** 가야 하지만, 요약 후보 생성(B-37 2단계)은 아직 없다. 지금 한 종류만
보내고, 생기면 같은 발송 시각·같은 이력 테이블에 얹는다 — 각자 타이머를 기르면
검토자는 하루에 DM 을 두 번 받는다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .attachment_review import PENDING, Attachment, find_sendable, scan

logger = logging.getLogger("tybot.daily_review")

KST = timezone(timedelta(hours=9))

KIND_ATTACHMENT = "attachment"
KIND_SUMMARY = "summary"

# 한 DM 에 건별로 보일 최대 건수. 넘으면 Slack 이 메시지를 자르고, 잘린 것을
# 사람은 모른다.
MAX_ROWS = 10


class DigestError(Exception):
    """하루치를 만들거나 보내지 못했다."""


@dataclass(frozen=True)
class Digest:
    """한 채널·한 사람에게 갈 하루치."""

    workspace: str
    channel_id: str
    channel_name: str
    recipient: str
    on: date
    today: list[Attachment]
    backlog: int
    # 변환본이 들어간 파일 이름. 화면이 「왜 급한가」 를 이 값으로 말한다.
    extracted: frozenset = frozenset()

    @property
    def empty(self) -> bool:
        return not self.today and not self.backlog


# --- 무엇이 실제로 막혔는가 ---------------------------------------------------
def blocked(
    archive_dir,
    *,
    workspace: str,
    channel_id: str,
    extracted: set[str],
) -> list[Attachment]:
    """이 채널에서 **승인 없이는 못 읽는** 첨부.

    판정은 답변 경로와 같은 함수(`find_sendable`)를 쓴다. 갈리면 이미 쓰이고 있는
    파일이 목록에 올라오고, 사람은 그 목록을 믿지 않게 된다.
    """
    out: list[Attachment] = []
    for item in scan(archive_dir):
        if item.workspace != workspace or item.channel_id != channel_id:
            continue
        if find_sendable(
            archive_dir,
            workspace=workspace,
            channel_id=channel_id,
            name=item.name,
            text_extracted=item.name in extracted,
        ):
            continue
        # 반려한 것은 사람이 이미 판단했다 — 매일 다시 물으면 그 판단이 무시된다.
        # 수집 실패(`failed`)도 뺀다. 파일이 우리에게 없으므로 승인해도 달라지는
        # 것이 없다. 그건 검토가 아니라 운영 문제고 진단 스크립트가 본다.
        if item.status != PENDING:
            continue
        out.append(item)
    return out


def staged_on(item: Attachment) -> date | None:
    """올라온 날(KST). 못 읽으면 `None` — **오늘로 치지 않는다.**

    모르는 것을 오늘로 치면 밀린 것이 매일 오늘 자리에 올라온다.
    """
    raw = (item.staged_at or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(KST).date()
    except ValueError:
        logger.warning("staged_at 을 읽지 못했다 file=%s", item.file_id)
        return None


def split(items: list[Attachment], on: date) -> tuple[list[Attachment], int]:
    """`(오늘 것, 밀린 건수)`.

    밀린 것을 건별로 늘어놓지 않는 이유는 사람의 눈이지 저장 공간이 아니다 —
    31건 사이에서 오늘의 2건을 찾게 하면 아무도 안 본다.
    """
    today = [i for i in items if staged_on(i) == on]
    return today, len(items) - len(today)


def build(
    archive_dir,
    *,
    workspace: str,
    channel_id: str,
    channel_name: str,
    recipient: str,
    extracted: set[str],
    on: date,
) -> Digest:
    items = blocked(
        archive_dir, workspace=workspace, channel_id=channel_id, extracted=extracted
    )
    today, backlog = split(items, on)
    return Digest(
        workspace=workspace,
        channel_id=channel_id,
        channel_name=channel_name,
        recipient=recipient,
        on=on,
        today=today,
        backlog=backlog,
        extracted=frozenset(extracted),
    )


# --- 화면 --------------------------------------------------------------------
def blocks(digest: Digest) -> list[dict]:
    """검토 DM. **버튼 `value` 를 비우지 않는다** — 비면 Slack 이 메시지를 통째로
    거부하고, 그건 오류가 아니라 「아무 일도 안 일어남」 으로 나타난다.

    값에는 **채널 ID 를 함께 싣는다.** DM 에서 누르면 `body["channel"]["id"]` 가
    DM 채널이라, 그것으로 원본을 찾으면 0건이 된다.
    """
    from . import attachment_view

    where = digest.channel_name or digest.channel_id
    head = (
        f"*{where} — 오늘 확인할 첨부 {len(digest.today)}건*\n"
        "이 파일들은 글자가 없어(스캔·이미지) 봇이 내용을 읽지 못했습니다. "
        "승인하면 원본이 LLM 제공자에게 전달됩니다 — 개인정보가 담긴 파일은 반려하세요."
    )
    out: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]

    rows = attachment_view.rows_for(digest.today[:MAX_ROWS], set(digest.extracted))
    out += attachment_view.blocks(rows, channel_id=digest.channel_id)[1:]

    tail = []
    if len(digest.today) > MAX_ROWS:
        tail.append(f"오늘 것 중 {len(digest.today) - MAX_ROWS}건은 다음에 보입니다.")
    if digest.backlog:
        # 건수만 말한다. 늘어놓으면 오늘 것이 묻힌다.
        tail.append(f"이전에 쌓인 {digest.backlog}건이 더 있습니다 — 채널에서 `/첨부`.")
    if tail:
        out.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " ".join(tail)}]}
        )
    return out


def text_fallback(digest: Digest) -> str:
    """알림 미리보기·접근성용. **파일명은 넣지 않는다** — 잠금화면에 뜬다."""
    return f"오늘 확인할 첨부 {len(digest.today)}건"


# --- 누구에게 ----------------------------------------------------------------
def recipients(workspace: str, channel_id: str, *, owner: str = "") -> list[str]:
    """검토자. 없으면 채널 개설자. 둘 다 없으면 빈 목록.

    빈 목록은 **호출부가 기록해야 할 사실**이다. 여기서 아무에게나 보내면 승인
    권한이 조용히 넓어진다.
    """
    from . import reviewers

    try:
        found = [r.reviewer_user for r in reviewers.reviewers_for(workspace, channel_id)]
    except reviewers.ReviewerError as exc:
        # 못 읽었을 때 개설자로 넘어가지 않는다 — 장애가 곧 권한 이동이 된다.
        logger.warning("검토자를 확인하지 못해 보내지 않는다 ch=%s: %s", channel_id, exc)
        return []
    if found:
        return found
    return [owner] if owner else []


def due(send_at: time, now: datetime) -> bool:
    """지금 보낼 시각인가. **그날 안이면 늦어도 보낸다.**

    지난 회의 알림과 다르다 — 어제 올라온 스캔본은 오늘 오후에 확인해도 쓸모가 있다.
    다만 날이 바뀌면 그날 몫은 버린다(다음 날 몫에 밀린 건수로 잡힌다).
    """
    return now.astimezone(KST).time() >= send_at


# --- 하루에 한 번 -------------------------------------------------------------
_APPLY = (
    "  sudo cat /opt/tybot/deploy/sql/review_digest_schema.sql "
    "| sudo -u postgres psql -p 55432 -d tyslackai -f -"
)

SCHEMA_MISSING = (
    "review_digest_sent 테이블이 없습니다. 스키마를 먼저 적용하세요:\n" + _APPLY
)

# 표는 있는데 못 쓴다. 조치가 다르므로 문장도 다르다 — "테이블이 없다" 를 보면
# 담당자는 표를 만들러 가고, 원인인 권한 누락은 그대로 남는다.
SCHEMA_DENIED = (
    "review_digest_sent 를 읽거나 쓸 수 없습니다({reason}). 표는 있으므로 만들 필요는"
    " 없고, 봇 역할에 권한이 없는 상태입니다. 아래를 실행하면 스키마 파일이 GRANT 까지"
    " 넣습니다:\n" + _APPLY
)


def schema_problem(conn) -> str:
    """이력 테이블을 **쓸 수 있는가.** 못 쓰면 할 일을 문장으로 돌려준다.

    예전에는 `to_regclass` 로 존재만 봤다. 그래서 표는 있는데 봇 역할에 GRANT 가
    없던 상태를 통과시켰고, 바로 다음 조회가 `permission denied` 로 끊겼다.
    **검사는 통과했는데 동작이 실패하는** 조합이고, 그건 "보낼 것이 없다" 와
    구별되지 않았다 — 2026-09-11 까지 검토 DM 이 한 건도 나가지 않은 원인이다.

    읽기만 시험하는 것으로는 부족하다. 보낸 뒤에 쓰기가 막히면 이력이 안 남아 같은
    사람에게 같은 DM 이 하루 종일 간다. 그래서 세이브포인트 안에서 쓰기까지 해 보고
    되돌린다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.review_digest_sent') AS t")
        row = cur.fetchone()
        present = row.get("t") if isinstance(row, dict) else (row[0] if row else None)
        if not present:
            return SCHEMA_MISSING
        try:
            cur.execute("SAVEPOINT probe_review_digest")
            cur.execute("SELECT 1 FROM review_digest_sent LIMIT 1")
            cur.execute(
                "INSERT INTO review_digest_sent"
                " (workspace, channel_id, recipient, digest_date, kind, item_count)"
                " VALUES ('__probe__', '__probe__', '__probe__', CURRENT_DATE,"
                "         'attachment', 0)"
            )
        except Exception as exc:  # noqa: BLE001 - 권한·컬럼 등 원인이 여럿이다
            cur.execute("ROLLBACK TO SAVEPOINT probe_review_digest")
            return SCHEMA_DENIED.format(reason=type(exc).__name__)
        cur.execute("ROLLBACK TO SAVEPOINT probe_review_digest")
    return ""


def schema_ready(conn) -> bool:
    """`schema_problem` 의 예/아니오 판."""
    return not schema_problem(conn)


def already_sent(conn, digest: Digest, *, kind: str = KIND_ATTACHMENT) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM review_digest_sent
             WHERE workspace = %s AND channel_id = %s AND recipient = %s
               AND digest_date = %s AND kind = %s
            """,
            (digest.workspace, digest.channel_id, digest.recipient, digest.on, kind),
        )
        return cur.fetchone() is not None


def mark_sent(conn, digest: Digest, *, kind: str = KIND_ATTACHMENT) -> None:
    """보낸 뒤에 남긴다. 보내기 전에 남기면 발송 실패가 「보냈음」 이 된다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO review_digest_sent
                   (workspace, channel_id, recipient, digest_date, kind, item_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                digest.workspace,
                digest.channel_id,
                digest.recipient,
                digest.on,
                kind,
                len(digest.today),
            ),
        )
    conn.commit()


# --- 발송 --------------------------------------------------------------------
@dataclass
class RunResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    no_recipient: int = 0


def send(client, digest: Digest) -> None:
    """검토자 DM. 본문·파일명은 로그에 남기지 않는다."""
    opened = client.conversations_open(users=digest.recipient)
    channel = (opened.get("channel") or {}).get("id")
    if not channel:
        raise DigestError("DM 채널을 열지 못했습니다.")
    client.chat_postMessage(
        channel=channel, blocks=blocks(digest), text=text_fallback(digest)
    )


def run(
    conn,
    clients: dict,
    *,
    archive_dir,
    channels,
    extracted_for,
    owners=None,
    now: datetime | None = None,
) -> RunResult:
    """보낼 것을 찾아 보낸다.

    `channels` 는 `[(워크스페이스, 채널ID, 채널명, 보낼시각), ...]` — 채널 목록은
    DB(`channel_reviewer`)가 알고, 이 함수는 Slack 을 묻지 않는다.
    `extracted_for(ws, ch) -> set[str]` 은 변환본이 들어간 파일 이름을 준다.
    """
    now = now or datetime.now(KST)
    today = now.astimezone(KST).date()
    owners = owners or {}
    result = RunResult()

    for workspace, channel_id, channel_name, send_at in channels:
        if not due(send_at, now):
            continue
        people = recipients(
            workspace, channel_id, owner=owners.get((workspace, channel_id), "")
        )
        if not people:
            # 조용히 넘어가지 않는다. 이 줄이 헬스 체크가 보는 것과 같은 사실이다.
            logger.warning(
                "검토자도 개설자도 없어 하루치를 보내지 못했다 ws=%s ch=%s",
                workspace, channel_id,
            )
            result.no_recipient += 1
            continue

        client = clients.get(workspace)
        if client is None:
            logger.warning("워크스페이스 클라이언트가 없다 ws=%s", workspace)
            result.failed += 1
            continue

        try:
            extracted = extracted_for(workspace, channel_id)
        except Exception as exc:  # noqa: BLE001 - 한 채널 실패가 나머지를 막지 않는다
            logger.warning("변환본 목록을 읽지 못했다 ws=%s ch=%s: %s",
                           workspace, channel_id, exc)
            result.failed += 1
            continue

        for person in people:
            digest = build(
                archive_dir,
                workspace=workspace,
                channel_id=channel_id,
                channel_name=channel_name,
                recipient=person,
                extracted=extracted,
                on=today,
            )
            if digest.empty:
                # 매일 "없습니다" 가 오면 사람이 이 DM 을 끈다.
                result.skipped += 1
                continue
            try:
                seen = already_sent(conn, digest)
            except Exception as exc:  # noqa: BLE001 - 한 사람 때문에 전원이 못 받으면 안 된다
                # 예전에는 이 조회가 그대로 터져 run() 밖으로 나갔다. 그러면 뒤에
                # 남은 채널·사람 전부가 못 받고, 로그에는 트레이스백 하나만 남는다.
                logger.warning(
                    "발송 이력을 읽지 못했다 ws=%s ch=%s: %s",
                    workspace, channel_id, type(exc).__name__,
                )
                result.failed += 1
                continue
            if seen:
                result.skipped += 1
                continue
            try:
                send(client, digest)
            except Exception as exc:  # noqa: BLE001 - Slack 오류 종류가 많다. 한 사람 실패로 멈추지 않는다
                logger.warning(
                    "하루치를 보내지 못했다 ws=%s ch=%s: %s",
                    workspace, channel_id, type(exc).__name__,
                )
                result.failed += 1
                continue
            try:
                mark_sent(conn, digest)
            except Exception as exc:  # noqa: BLE001 - 이미 보냈다. 여기서 멈추면 더 나쁘다
                # 보낸 뒤에 이력이 안 남으면 다음 회차에 같은 DM 이 또 간다. 그래도
                # 나머지 사람을 못 보내게 하는 쪽이 더 나쁘므로 계속한다.
                logger.error(
                    "보냈지만 이력을 남기지 못했다 — 중복 발송이 생길 수 있다"
                    " ws=%s ch=%s: %s",
                    workspace, channel_id, type(exc).__name__,
                )
            logger.info(
                "하루치 발송 ws=%s ch=%s 오늘=%d 밀림=%d",
                workspace, channel_id, len(digest.today), digest.backlog,
            )
            result.sent += 1
    return result


# --- 실행 --------------------------------------------------------------------
def _channels(conn) -> list[tuple[str, str, str, time]]:
    """검토자가 지정된 채널과 그 발송 시각. 채널마다 한 줄."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT workspace, channel_id,
                   max(channel_name) AS channel_name,
                   min(send_at)      AS send_at
              FROM channel_reviewer
             WHERE enabled
             GROUP BY workspace, channel_id
            """
        )
        return [
            (str(r["workspace"]), str(r["channel_id"]),
             str(r["channel_name"] or ""), r["send_at"])
            for r in cur.fetchall()
        ]


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="검토자에게 하루치 첨부 검수를 보낸다")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 건수만 센다")
    args = ap.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")

    from .envfile import load_env_file

    load_env_file()

    if not os.getenv("DATABASE_URL"):
        logger.error("DATABASE_URL 이 없다. 검토자는 DB 에 있다.")
        return 2

    from . import heartbeat
    from .archive.store import ArchiveStore
    from .channel_management import ChannelOwnerStore
    from .paths import archive_dir
    from .workspaces import ConfigError, load_workspaces

    try:
        configs = load_workspaces()
    except ConfigError as exc:
        logger.error("워크스페이스 설정 오류: %s", exc)
        return 2

    root = archive_dir()
    store = ArchiveStore(root)

    def extracted_for(workspace: str, channel_id: str) -> set[str]:
        from .answer import EXTRACTED_ATTACHMENT_RE as pattern

        return {
            m.group("name")
            for doc in store.docs()
            if doc.channel_id == channel_id
            for line in doc.raw_lines
            if (m := pattern.match((line.text or "").strip()))
        }

    import psycopg

    with psycopg.connect(
        os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row
    ) as conn:
        problem = schema_problem(conn)
        if problem:
            logger.error("%s", problem)
            return 2
        channels = _channels(conn)
        if args.dry_run:
            clients = {}
        else:
            from slack_sdk import WebClient

            clients = {c.key: WebClient(token=c.bot_token) for c in configs}
        result = run(
            conn,
            clients,
            archive_dir=root,
            channels=channels,
            extracted_for=extracted_for,
            # 검토자를 안 정한 채널은 개설자에게 간다. 안 그러면 그 채널 첨부는
            # 아무도 확인하지 않고, 아무 일도 안 일어난다.
            owners=ChannelOwnerStore(
                heartbeat.state_dir() / "channel-owners.json"
            ).owners(),
        )

    logger.info(
        "검토 하루치 sent=%d skipped=%d failed=%d 받는사람없음=%d",
        result.sent, result.skipped, result.failed, result.no_recipient,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
