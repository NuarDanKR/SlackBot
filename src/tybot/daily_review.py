"""검토자에게 요약 후보를 밀어 준다 — 사람이 찾아오게 하지 않는다.

설계: [`docs/design/summary-review.md`](../../docs/design/summary-review.md) 3단계

## 왜 밀어 주는가 (2026-09-08, 오너 결정)

첨부 처리를 별도 명령으로만 열어 뒀을 때 결과는 **대기 31건·처리 0건**이었다.

> "사용자는 이 첨부 파일이 승인이 필요한지도 모를거고 실패한지도 모를거야."

맞다. 사람이 **모르는 일을 하러 찾아오지는 않는다.** 당겨 가는 방식은 게이트가
아니라 정체였다. 그래서 두 가지를 바꿨다.

1. 변환은 사람 승인을 기다리지 않는다. 추출 텍스트가 아카이브에 들어갔다는 것이
   곧 수집 단계 PII 검사를 통과했다는 뜻이다.
2. 변환 상세는 검토자 DM으로 보내지 않는다. 성공한 변환본은 요약 후보의 근거에
   포함되고, 실패·미지원 파일은 관리 콘솔의 아카이브 진단에서 운영자가 확인한다.

밀어 준 것을 놓쳤거나 실패가 누적되면 관리 콘솔의 아카이브 진단에서 다시 본다.

## 지키는 것 다섯

**검토할 정보가 있는 것만 담는다.** 변환 완료는 정제된 미리보기를, 실패는 조치 사유를,
읽지 못한 파일은 원본 전송 여부를 보여 준다. PII로 수집 제외된 본문은 보내지 않는다.

**오늘 것과 밀린 것을 섞지 않는다.** 밀린 31건 사이에 오늘 올라온 2건을 끼워 넣으면
오늘 것이 묻힌다. 오늘 것을 건별로 보이고, 밀린 것은 **건수 한 줄**로만 말한다.

**하루에 한 번.** 멱등 키는 `(워크스페이스, 채널, 받는 사람, 날짜, 종류)` 다.
타이머가 1분마다 돌아도 두 번 가지 않는다.

**검토자와 채널 담당자 모두에게.** 같은 사람이 양쪽에 있으면 한 번만 보낸다. 아무도
없으면 **보내지 않고 그 사실을 남긴다** — 조용한 실패를 만들지 않는다.

**보낼 것이 없으면 보내지 않는다.** 매일 "없습니다" 가 오면 사람이 그 DM 을 끈다.

## 첨부 하루치 코드를 남겨 둔 이유

기존 첨부 발송 이력과 콘솔 진단 호환을 위해 첨부 하루치 생성 함수는 남아 있다.
운영 실행기에서는 호출하지 않는다. 검토자에게 발송하는 것은 Hermes 계약으로 만들고
원문 대조를 통과한 요약 후보뿐이다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .attachment_review import (
    APPROVED,
    PII_REFUSED,
    REJECTED,
    Attachment,
    scan,
)

logger = logging.getLogger("tybot.daily_review")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
    """이 채널에서 자동 변환되지 않아 운영 확인이 필요한 첨부."""
    out: list[Attachment] = []
    for item in scan(archive_dir):
        if item.workspace != workspace or item.channel_id != channel_id:
            continue
        if item.extracted or item.name in extracted:
            continue
        if item.status in {APPROVED, REJECTED, PII_REFUSED}:
            continue
        out.append(item)
    return out


def review_items(
    archive_dir,
    *,
    workspace: str,
    channel_id: str,
) -> list[Attachment]:
    """검토자가 알아야 할 자동 변환 결과, 실패 및 원본 검토 대기 항목."""
    return [
        item
        for item in scan(archive_dir)
        if item.workspace == workspace
        and item.channel_id == channel_id
        and item.status not in {APPROVED, REJECTED, PII_REFUSED}
    ]


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
    items = review_items(archive_dir, workspace=workspace, channel_id=channel_id)
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
    """자동 변환 결과와 실패를 보여 주는 검토 DM."""
    from . import attachment_view

    where = digest.channel_name or digest.channel_id
    converted = sum(
        1 for item in digest.today if item.extracted or item.name in digest.extracted
    )
    failed = sum(1 for item in digest.today if item.conversion_failed)
    unreadable = len(digest.today) - converted - failed
    head = (
        f"*{where} — 오늘 첨부 처리 {len(digest.today)}건*\n"
        f"자동 변환 {converted}건 · 변환 실패 {failed}건 · 원본 확인 필요 {unreadable}건\n"
        "변환된 내용은 아래에서 확인할 수 있습니다. 실패한 파일은 외부 LLM에 보내지 "
        "않으며 Slack 원본과 콘솔 진단에서 조치합니다."
    )
    out: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]

    rows = attachment_view.rows_for(digest.today[:MAX_ROWS], set(digest.extracted))
    out += attachment_view.blocks(rows, channel_id=digest.channel_id)[1:]

    tail = []
    if len(digest.today) > MAX_ROWS:
        tail.append(f"오늘 것 중 {len(digest.today) - MAX_ROWS}건은 다음에 보입니다.")
    if digest.backlog:
        # 건수만 말한다. 늘어놓으면 오늘 것이 묻힌다.
        tail.append(
            f"이전에 쌓인 {digest.backlog}건이 더 있습니다 — 관리 콘솔의 아카이브 진단에서 확인하세요."
        )
    if tail:
        out.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " ".join(tail)}]}
        )
    return out


def text_fallback(digest: Digest) -> str:
    """알림 미리보기·접근성용. **파일명은 넣지 않는다** — 잠금화면에 뜬다."""
    return f"오늘 확인할 첨부 {len(digest.today)}건"


# --- 누구에게 ----------------------------------------------------------------
def recipients(
    workspace: str,
    channel_id: str,
    *,
    owner: str | tuple[str, ...] | list[str] = "",
) -> list[str]:
    """검토자와 채널 담당자. 둘 다 없으면 빈 목록.

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
    responsible = [owner] if isinstance(owner, str) else list(owner)
    return list(dict.fromkeys([*found, *(user for user in responsible if user)]))


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


def last_sent(conn, *, workspace: str, channel_id: str) -> str:
    """이 채널로 검토 DM 이 마지막으로 나간 날(KST). 한 번도 없으면 빈 문자열.

    「검토자 있음」 만으로는 발송이 살아 있는지 알 수 없다. 검토자 지정은 사람이
    하는 일이고 발송은 서버가 하는 일이라 따로 고장난다 — 2026-09-11 에 권한 누락과
    타이머 미기동이 동시에 있었고, 화면은 초록이었다.

    받는 사람은 묻지 않는다. 「이 채널로 나갔나」 가 알고 싶은 것이다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(digest_date) AS d FROM review_digest_sent"
            " WHERE workspace = %s AND channel_id = %s",
            (workspace, channel_id),
        )
        row = cur.fetchone()
    value = row.get("d") if isinstance(row, dict) else (row[0] if row else None)
    return value.isoformat() if value else ""


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
                "검토자도 채널 담당자도 없어 하루치를 보내지 못했다 ws=%s ch=%s",
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


def _archive_channels(archive) -> list[tuple[str, str, str, time]]:
    """아카이브에 원문이 있는 **모든** 채널. 검토자 유무를 묻지 않는다(B-62).

    평소 채널 목록은 `channel_reviewer` 가 안다 — 보낼 사람이 있는 채널만 도는 것이
    맞기 때문이다. 그런데 검토자 지정은 채널마다 업무 협의가 필요해 몇 주가 걸리고,
    그동안 **후보 생성까지 멈춰 있을 이유는 없다.** 생성은 사람에게 아무것도 보내지
    않는다.

    발송 시각은 자정으로 둔다. 이 목록은 보내지 않는 회차에만 쓰인다.
    """
    seen: dict[tuple[str, str], str] = {}
    for doc in archive.docs():
        channel_id = str(doc.channel_id or "")
        if not channel_id or not doc.raw_lines:
            continue
        seen.setdefault((doc.workspace, channel_id), doc.channel or "")
    return [
        (workspace, channel_id, name, time.min)
        for (workspace, channel_id), name in sorted(seen.items())
    ]


def _force_target(
    channels: list[tuple[str, str, str, time]],
    targets: list[tuple[str, str]],
) -> list[tuple[str, str, str, time]]:
    """즉시 실행할 채널만 남기고 예약 시각을 자정으로 바꾼다.

    대상은 여러 개일 수 있다. 콘솔에서 채널을 여러 개 고르면 한 번의 실행으로
    돌아야 한다 — 채널마다 작업을 따로 띄우면 「한 번에 하나」 잠금에 걸린다.
    """
    wanted = set(targets)
    return [
        (ws, channel, name, time.min)
        for ws, channel, name, _send_at in channels
        if (ws, channel) in wanted
    ]


def parse_target(value: str) -> tuple[str, str]:
    """`워크스페이스:채널ID` 를 쪼갠다. 형식이 아니면 `ValueError`."""
    workspace, separator, channel_id = value.partition(":")
    if not separator or not workspace.strip() or not channel_id.strip():
        raise ValueError(f"대상 형식이 올바르지 않습니다: {value}")
    return workspace.strip(), channel_id.strip()


# 검토 설정 자체가 없는 채널. `summary_review` 는 이 채널을 보지도 못하므로
# 여기서 결과에 넣는다 — 목록에서 사라지면 「보냈다」 로 오해된다.
NO_REVIEW_CONFIG = "no-review-config"


def _write_result(path: str, result, *, unconfigured: list[tuple[str, str]],
                  generate_only: bool = False) -> None:
    """채널별 결과를 JSON 으로 남긴다. **종료 코드는 이걸 대신하지 못한다.**

    보낼 것이 없어도, 검토자가 없어도, 이미 보냈어도 실행은 0 으로 끝난다. 그래서
    콘솔이 「실행 완료」 만 보이고 아무도 DM 을 못 받는 일이 생겼다(2026-09-17).
    """
    import json
    from pathlib import Path

    outcomes = [row.as_dict() for row in (result.outcomes if result else [])]
    outcomes += [
        {
            "workspace": workspace,
            "channelId": channel_id,
            "channelName": "",
            "code": NO_REVIEW_CONFIG,
            # 모드마다 없는 것이 다르다. 「검토 설정이 없다」 를 후보만 만드는
            # 회차에 보이면 검토자를 지정하러 가는데, 없는 것은 원문이다.
            "reason": (
                "아카이브에 원문이 없는 채널입니다"
                if generate_only else "활성 검토 설정이 없는 채널입니다"
            ),
            "sent": 0,
            "skipped": 0,
            "failed": 1,
        }
        for workspace, channel_id in unconfigured
    ]
    payload = {
        "sent": sum(row["sent"] for row in outcomes),
        "skipped": sum(row["skipped"] for row in outcomes),
        "failed": sum(row["failed"] for row in outcomes),
        "generated": result.generated if result else 0,
        "canvasFallback": result.canvas_fallback if result else 0,
        "generateOnly": bool(generate_only),
        "channels": outcomes,
    }
    try:
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.error("실행 결과를 남기지 못했습니다: %s", exc)


def _report_estimate(archive, channels, *, since: str, result_path: str | None) -> None:
    """소급 대상 분량을 세어 로그와 결과 파일에 남긴다. LLM 은 부르지 않는다."""
    import json
    from pathlib import Path

    from . import summary_review

    rows = []
    for workspace, channel_id, channel_name, _send_at in channels:
        found = summary_review.backfill_estimate(
            archive, workspace=workspace, channel_id=channel_id, since=since,
        )
        logger.info(
            "소급 대상 ws=%s ch=%s 줄=%d 회차=%d 기간=%s~%s",
            workspace, channel_id, found.lines, found.rounds,
            found.first_at or "-", found.last_at or "-",
        )
        rows.append({
            "workspace": workspace,
            "channelId": channel_id,
            "channelName": channel_name,
            "code": "estimate",
            "reason": (
                f"소급 대상 {found.lines}줄 · 예상 {found.rounds}회차"
                if found.lines else "소급할 원문이 없습니다"
            ),
            "lines": found.lines,
            "rounds": found.rounds,
            "firstAt": found.first_at,
            "lastAt": found.last_at,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
        })
    if not result_path:
        return
    payload = {"sent": 0, "skipped": 0, "failed": 0, "generated": 0,
               "canvasFallback": 0, "estimate": True, "channels": rows}
    try:
        Path(result_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.error("소급 추정 결과를 남기지 못했습니다: %s", exc)


def _run_backfill(summary_review, conn, archive, channels, *, complete, now,
                  since: str, rounds: int, resume: bool) -> None:
    """수집된 과거 원문으로 검토 후보를 소급 생성한다.

    한 채널이 실패해도 나머지 채널은 돈다 — 소급은 여러 달치를 한 번에 도는 일이라
    중간에 멈추면 어디까지 됐는지 아무도 모른다. 커서는 회차마다 저장되므로 다시
    실행하면 멈춘 지점부터 이어 간다.
    """
    store = summary_review.Store(conn)
    for workspace, channel_id, channel_name, _send_at in channels:
        try:
            done = summary_review.backfill_channel(
                store, archive, workspace=workspace, channel_id=channel_id,
                channel_name=channel_name, complete=complete, now=now,
                since=since, max_rounds=rounds, resume=resume,
            )
        except Exception as exc:  # noqa: BLE001 - 한 채널 실패가 나머지를 막지 않는다
            logger.warning(
                "소급 검토 실패 ws=%s ch=%s code=%s",
                workspace, channel_id, type(exc).__name__,
            )
            continue
        logger.warning(
            "소급 검토 ws=%s ch=%s 회차=%d 후보=%d 남은원문=%s",
            workspace, channel_id, done.rounds, done.candidates,
            "없음" if done.exhausted else "있음(다시 실행하면 이어 갑니다)",
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="검토자에게 요약 후보 검토를 보낸다")
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 건수만 센다")
    ap.add_argument(
        "--force-now",
        action="store_true",
        help="지정한 채널의 예약 시각만 우회한다(오늘 발송 이력은 우회하지 않음)",
    )
    ap.add_argument("--workspace", help="--force-now 대상 워크스페이스 키")
    ap.add_argument("--channel", action="append", help="--force-now 대상 Slack 채널 ID(반복 가능)")
    ap.add_argument(
        "--target",
        action="append",
        help="--force-now 대상을 `워크스페이스:채널ID` 로 지정한다(반복 가능)",
    )
    ap.add_argument(
        "--resend",
        action="store_true",
        help="오늘 이미 보낸 검토자에게도 다시 보낸다(--force-now 전용)",
    )
    ap.add_argument("--result-json", help="채널별 실행 결과를 이 경로에 JSON 으로 남긴다")
    ap.add_argument(
        "--deliver-only",
        action="store_true",
        help="LLM 을 부르지 않고 이미 만들어 둔 후보만 다시 보낸다(--force-now 전용)",
    )
    ap.add_argument(
        "--generate-only",
        action="store_true",
        help="아무에게도 보내지 않고 후보만 만든다. 검토자가 없는 채널도 돈다",
    )
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="이미 수집된 과거 원문으로 검토 후보를 소급 생성한다(--force-now 전용)",
    )
    ap.add_argument(
        "--since",
        default="",
        help="소급 시작일 `YYYY-MM-DD`. 비우면 아카이브 처음부터",
    )
    ap.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="채널당 소급 회차 상한. 회차마다 LLM 을 한 번 부른다",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="커서를 되돌리지 않고 지난 소급이 멈춘 지점부터 이어 간다",
    )
    ap.add_argument(
        "--estimate",
        action="store_true",
        help="LLM 을 부르지 않고 소급 대상 분량만 센다",
    )
    args = ap.parse_args(argv)

    targets: list[tuple[str, str]] = []
    for value in args.target or []:
        try:
            targets.append(parse_target(value))
        except ValueError as exc:
            ap.error(str(exc))
    for channel in args.channel or []:
        if not args.workspace:
            ap.error("--channel 에는 --workspace 가 필요합니다. 또는 --target 을 쓰세요.")
        targets.append((args.workspace, channel))
    # 같은 채널을 두 번 적어도 한 번만 돈다 — 두 번 돌면 재발송이 두 번 간다.
    targets = list(dict.fromkeys(targets))

    # 대상을 직접 고르는 회차인가. 둘 다 사람이 직접 실행하는 경로다.
    manual = args.force_now or args.generate_only
    if args.force_now and not targets:
        ap.error("--force-now에는 --target 또는 --workspace/--channel이 필요합니다.")
    if not manual and (targets or args.workspace):
        ap.error("--target/--workspace/--channel은 --force-now 또는 --generate-only와 함께 사용하세요.")
    if args.resend and not args.force_now:
        ap.error("--resend는 --force-now와 함께만 사용합니다.")
    if args.force_now and args.dry_run:
        ap.error("--force-now와 --dry-run은 함께 사용할 수 없습니다.")
    if args.backfill and not manual:
        ap.error("--backfill은 --force-now 또는 --generate-only와 함께만 사용합니다.")
    if args.backfill and args.generate_only and not targets:
        # 소급은 회차마다 LLM 을 한 번 부른다. 채널 수십 개를 암묵적으로 도는 것은
        # 일일 비용 한도를 한 번에 쓰는 일이라 **명시적으로 고르게 한다.**
        ap.error("--generate-only로 소급할 때는 --target으로 채널을 고르세요.")
    if args.deliver_only and not args.force_now:
        ap.error("--deliver-only는 --force-now와 함께만 사용합니다.")
    if args.deliver_only and args.backfill:
        ap.error("--deliver-only는 후보를 만들지 않으므로 --backfill과 함께 쓸 수 없습니다.")
    if args.generate_only and args.deliver_only:
        ap.error("--generate-only와 --deliver-only는 정반대 동작입니다.")
    if args.generate_only and args.dry_run:
        ap.error("--generate-only와 --dry-run은 함께 사용할 수 없습니다.")
    if args.generate_only and args.resend:
        ap.error("--generate-only는 아무것도 보내지 않으므로 --resend를 쓰지 않습니다.")
    for flag, value in (("--since", args.since), ("--rounds", args.rounds),
                        ("--resume", args.resume), ("--estimate", args.estimate)):
        if value and not args.backfill:
            ap.error(f"{flag}는 --backfill과 함께만 사용합니다.")
    if args.since and not _DATE_RE.fullmatch(args.since):
        ap.error("--since는 `YYYY-MM-DD` 형식입니다.")
    if args.since and args.resume:
        ap.error("--resume은 시작점을 되돌리지 않으므로 --since와 함께 쓸 수 없습니다.")

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

    import psycopg

    with psycopg.connect(
        os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row
    ) as conn:
        problem = schema_problem(conn)
        if problem:
            logger.error("%s", problem)
            return 2
        # 검토자가 없는 채널도 후보는 만들 수 있다(B-62). 보내지 않는 회차에서만
        # 목록을 넓힌다 — 평소 회차가 이 목록을 쓰면 보낼 사람 없는 채널을 매번 돈다.
        channels = _archive_channels(store) if args.generate_only else _channels(conn)
        unconfigured: list[tuple[str, str]] = []
        if targets:
            channels = _force_target(channels, targets)
            found = {(ws, channel) for ws, channel, _name, _at in channels}
            unconfigured = [item for item in targets if item not in found]
            for workspace, channel_id in unconfigured:
                logger.error(
                    "%s ws=%s ch=%s",
                    "아카이브에 원문이 없습니다" if args.generate_only
                    else "활성 검토 설정을 찾지 못했습니다",
                    workspace, channel_id,
                )
            if not channels:
                if args.result_json:
                    _write_result(args.result_json, None, unconfigured=unconfigured,
                                  generate_only=args.generate_only)
                return 2
            logger.warning(
                "예약 시각을 우회해 요약 검토를 즉시 실행합니다 대상=%d개%s",
                len(channels),
                " (재발송)" if args.resend else "",
            )
        if args.generate_only:
            logger.warning(
                "후보만 만들고 아무에게도 보내지 않습니다 대상=%d개", len(channels),
            )
        if args.estimate:
            # LLM 을 부르지 않는다. 얼마나 되는지 보고 누르라고 있는 경로다.
            _report_estimate(store, channels, since=args.since,
                             result_path=args.result_json)
            return 0
        if args.dry_run:
            clients = {}
        else:
            from slack_sdk import WebClient

            clients = {c.key: WebClient(token=c.bot_token) for c in configs}
        owners = ChannelOwnerStore(
            heartbeat.state_dir() / "channel-owners.json"
        ).responsibles()
        summary_result = None
        if not args.dry_run:
            from . import summary_review
            from .config import cost_state_path
            from .gateway import cost
            from .gateway.base import Message, Sensitivity
            from .gateway.budget import WorkspaceLimits
            from .gateway.router import Router

            router = Router.from_default_registry(
                daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
                default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
                cost_state_path=cost_state_path(os.getenv("QA_LOG_DIR", "./qa-log")),
                workspace_limits=WorkspaceLimits(),
            )

            def complete_summary(system: str, user: str, workspace: str) -> str:
                with cost.attribute_to(workspace):
                    response = router.complete(
                        [Message("system", system), Message("user", user)],
                        sensitivity=Sensitivity.CONFIDENTIAL,
                        max_tokens=4096,
                    )
                return response.text

            if args.backfill:
                _run_backfill(
                    summary_review, conn, store, channels,
                    complete=complete_summary, now=datetime.now(KST),
                    since=args.since, rounds=args.rounds, resume=args.resume,
                )

            summary_result = summary_review.run(
                conn,
                clients,
                archive=store,
                channels=channels,
                owners=owners,
                complete=complete_summary,
                resend=args.resend,
                deliver_only=args.deliver_only,
                generate_only=args.generate_only,
                # 콘솔의 즉시 실행은 오늘 이미 생성기를 돌렸더라도 워터마크 이후
                # 새 원문을 다시 확인한다. 예약 실행은 하루 한 번 잠금을 유지한다.
                force_generate=args.force_now,
            )

    if args.result_json:
        _write_result(args.result_json, summary_result, unconfigured=unconfigured,
                      generate_only=args.generate_only)

    if summary_result is not None:
        # Canvas 수치를 **따로** 남긴다. 폴백을 성공과 합치면 Canvas 가 계속
        # 실패하는데도 「잘 보내지고 있다」 로 보인다(B-50).
        logger.info(
            "요약 검토 generated=%d sent=%d skipped=%d failed=%d "
            "canvas=%d fallback=%d ambiguous=%d expired=%d",
            summary_result.generated,
            summary_result.sent,
            summary_result.skipped,
            summary_result.failed,
            summary_result.canvas_created,
            summary_result.canvas_fallback,
            summary_result.canvas_ambiguous,
            summary_result.expired,
        )
        for outcome in summary_result.outcomes:
            logger.info(
                "요약 검토 채널 ws=%s ch=%s result=%s sent=%d skipped=%d failed=%d",
                outcome.workspace,
                outcome.channel_id,
                outcome.code,
                outcome.sent,
                outcome.skipped,
                outcome.failed,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
