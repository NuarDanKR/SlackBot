"""첨부 변환 실패·부분 누락을 **채널 단위로 묶어** 알린다 (B-45 §6).

설계: [`operational-warning-recovery-and-answer-progress.md`](../../docs/design/operational-warning-recovery-and-answer-progress.md)

## 언제 알리나

**검토를 막을 때만.** 자동으로 다시 해 볼 것이 남아 있으면 알리지 않는다 —
큐가 몇 분 뒤 성공할 수도 있는데 그때마다 DM 이 가면, 사람은 곧 그 DM 을 읽지
않게 되고 정작 중요한 한 건도 같이 묻힌다.

| 상태 | 알리나 |
|---|---|
| 큐에서 재시도 중(`queued`) | 아니오 — 기다리면 된다 |
| 첫 일시 실패 | 아니오 — 운영 기록이다 |
| `failed` (더 해 볼 것이 없다) | 예 |
| `held` (환경 문제로 멈춤) | 예 |
| `partial` (일부만 읽음) | 예 — **답이 나가기 때문이다.** 범위를 모르면 사람은 전부 본 줄 안다 |
| `pii_refused` | 아니오 — 정책이다. 고칠 것이 없다 |

## 무엇을 담나

파일명, Slack 원본 링크, 비민감 오류 분류, 다음 조치, 확인 범위. **그뿐이다.**
본문·질문·요약·토큰은 담지도 저장하지도 않는다.

## 왜 채널 단위인가

파일마다 DM 을 보내면 한 채널에서 20건이 실패한 날 DM 이 20개 간다. 받는 사람은
그날 이후로 그 DM 을 읽지 않는다. 묶으면 한 번에 읽히고, 무엇이 밀렸는지 한눈에
보인다.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger("tybot.conversion_alerts")

# 알릴 상태. `queued` 는 없다 — 기다리면 된다.
ALERT_STATES = ("failed", "held", "partial")

# 한 DM 에 담을 파일 수. 넘으면 개수만 밝힌다 — 200줄짜리 DM 은 아무도 안 읽는다.
MAX_FILES_IN_MESSAGE = 15

# 다음 조치. **코드마다 사람이 할 일이 다르다** — 그것을 안 적으면
# "변환 실패" 만 보이고 무엇을 해야 하는지 모른다.
NEXT_STEPS = {
    "converter_missing": "서버에 문서 변환기가 없습니다. 관리자에게 알려 주세요.",
    "converter_policy_denied": "서버 정책이 변환기를 막고 있습니다. 관리자 확인이 필요합니다.",
    "converter_crashed": "변환기가 비정상 종료했습니다. 관리자 확인이 필요합니다.",
    "converter_timeout": "변환이 시간 안에 끝나지 않았습니다. 파일을 나눠 올려 보세요.",
    "encrypted": "암호가 걸린 파일입니다. 암호를 풀어 다시 올려 주세요.",
    "corrupt": "파일이 손상됐습니다. 원본을 다시 올려 주세요.",
    "unsupported": "지원하지 않는 형식입니다. PDF 나 Office 형식으로 다시 올려 주세요.",
    "empty_output": "본문을 찾지 못했습니다. 스캔본이면 원본 확인이 필요합니다.",
    "original_missing": "원본이 남아 있지 않습니다. 채널에 다시 올려 주세요.",
}
DEFAULT_NEXT_STEP = "원본을 열어 확인하거나, 관리자에게 재처리를 요청하세요."
PARTIAL_NEXT_STEP = "읽지 못한 범위는 원본에서 확인해 주세요."


class AlertError(RuntimeError):
    """알림을 보낼 수 없다. **변환 큐 상태를 되돌리는 이유가 되지 않는다.**"""


@dataclass(frozen=True)
class AlertItem:
    """한 파일. **본문은 없다** — 좌표와 코드, 그리고 사람이 읽을 이름뿐이다."""

    file_id: str
    name: str
    state: str  # failed | held | partial
    error_code: str = ""
    coverage_note: str = ""
    permalink: str = ""

    @property
    def next_step(self) -> str:
        if self.state == "partial":
            return PARTIAL_NEXT_STEP
        return NEXT_STEPS.get(self.error_code, DEFAULT_NEXT_STEP)

    def line(self) -> str:
        """Slack 한 줄. **원본 링크가 없으면 만들어 내지 않는다.**"""
        label = f"<{self.permalink}|{self.name}>" if self.permalink else self.name
        bits = [label]
        if self.state == "partial":
            bits.append(f"일부만 읽음{f' ({self.coverage_note})' if self.coverage_note else ''}")
        elif self.state == "held":
            bits.append(f"보류: {self.error_code or '환경 확인 필요'}")
        else:
            bits.append(f"실패: {self.error_code or '원인 미상'}")
        return "• " + " — ".join(bits)


@dataclass(frozen=True)
class ChannelAlert:
    """한 채널에 대한 알림 하나. 받는 사람마다 이것을 한 번씩 보낸다."""

    workspace: str
    channel_id: str
    channel_name: str
    items: tuple[AlertItem, ...]
    pipeline_version: str = "1"

    @property
    def dedupe_key(self) -> str:
        """같은 상태면 다시 안 보낸다.

        상태가 **바뀌면** 새 키가 된다 — 실패가 부분 성공이 되거나 파일이 늘면
        다시 알릴 값이 있다. 파일명은 키에 넣지 않는다(이름이 바뀌어도 같은 건).
        """
        parts = sorted(f"{i.file_id}:{i.state}:{i.error_code}" for i in self.items)
        raw = "\x00".join([self.workspace, self.channel_id, self.pipeline_version, *parts])
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def text(self) -> str:
        head = (
            f"*{self.channel_name or self.channel_id}* 첨부 {len(self.items)}건이 "
            "검토에 쓸 수 없는 상태입니다."
        )
        lines = [item.line() for item in self.items[:MAX_FILES_IN_MESSAGE]]
        if len(self.items) > MAX_FILES_IN_MESSAGE:
            lines.append(f"… 외 {len(self.items) - MAX_FILES_IN_MESSAGE}건")
        steps = list(dict.fromkeys(item.next_step for item in self.items))
        return "\n".join([head, "", *lines, "", "*다음 조치*", *[f"• {s}" for s in steps]])

    def log_line(self) -> str:
        """업무 내용 없이 좌표와 개수만."""
        states = ",".join(sorted({i.state for i in self.items}))
        return (
            f"alert ws={self.workspace} ch={self.channel_id} "
            f"files={len(self.items)} states={states} key={self.dedupe_key[:8]}"
        )


@dataclass
class RunResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    no_recipient: int = 0
    channels: list[str] = field(default_factory=list)


# --- 무엇을 알릴 것인가 -------------------------------------------------------


def alert_state(item) -> str:
    """이 첨부가 알릴 상태인가. 아니면 빈 문자열.

    **큐에서 아직 돌 수 있는 것은 알리지 않는다.** 재시도로 성공할 수 있는데
    알리면, 곧 성공할 일로 사람을 부르는 셈이다.
    """
    from .attachment_review import PII_REFUSED

    if item.status == PII_REFUSED:
        # 정책 제외다. 고칠 것이 없으므로 알릴 것도 없다.
        return ""
    if getattr(item, "conversion_state", "") == "partial":
        return "partial"
    if not item.conversion_failed:
        return ""
    if item.retryable:
        # 큐가 다시 해 본다. 다 써 버린 뒤에 알린다.
        return ""
    return "failed"


def collect(archive_dir, *, queue_state=None) -> list[ChannelAlert]:
    """알릴 것을 채널별로 묶는다.

    `queue_state` 는 `(workspace, channel_id, file_id) -> 큐 상태` 를 주는 함수다.
    `held` 는 큐만 아는 상태라 metadata 로는 판별할 수 없다. 없으면 생략한다 —
    **큐를 못 읽는다고 알림 전체가 멈추면 안 된다.**
    """
    from .attachment_review import scan

    grouped: dict[tuple[str, str], list[AlertItem]] = {}
    for item in scan(archive_dir):
        state = alert_state(item)
        if queue_state is not None:
            try:
                got = queue_state(item.workspace, item.channel_id, item.file_id)
            except Exception as exc:  # noqa: BLE001 - 큐 장애가 알림을 막지 않는다
                log.warning("큐 상태를 읽지 못했습니다: %s", exc)
                got = None
            if got == "held":
                state = "held"
            elif got == "queued" and state == "failed":
                # 아직 재시도가 남아 있다.
                state = ""
        if not state:
            continue
        grouped.setdefault((item.workspace, item.channel_id), []).append(
            AlertItem(
                file_id=item.file_id,
                name=item.name or item.file_id,
                state=state,
                error_code=item.error_code,
                coverage_note=getattr(item, "coverage_note", ""),
                permalink=item.permalink,
            )
        )
    return [
        ChannelAlert(
            workspace=workspace,
            channel_id=channel_id,
            channel_name="",
            items=tuple(sorted(items, key=lambda i: i.name)),
        )
        for (workspace, channel_id), items in sorted(grouped.items())
    ]


# --- 누구에게 ----------------------------------------------------------------


def recipients(workspace: str, channel_id: str, *, owner_of=None) -> list[str]:
    """채널 관리자와 검토자의 **저장된 사용자 ID**.

    Slack 표시명으로 추측하지 않는다 — 같은 이름이 둘이면 남에게 간다.
    """
    out: list[str] = []
    try:
        from .reviewers import reviewers_for

        out.extend(r.reviewer_user for r in reviewers_for(workspace, channel_id) if r.enabled)
    except Exception as exc:  # noqa: BLE001 - 한쪽을 못 읽어도 다른 쪽에는 간다
        log.warning("검토자를 읽지 못했습니다 ws=%s ch=%s: %s", workspace, channel_id, exc)
    if owner_of is not None:
        try:
            owner = owner_of(workspace, channel_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("채널 관리자를 읽지 못했습니다: %s", exc)
            owner = ""
        if isinstance(owner, str):
            if owner:
                out.append(owner)
        else:
            out.extend(str(user) for user in (owner or []) if str(user))
    return list(dict.fromkeys(user for user in out if user))


def may_see_channel(client, channel_id: str, user: str) -> bool:
    """지금도 이 채널을 볼 수 있는가.

    **발송 직전에 다시 본다.** 검토자로 등록된 뒤 채널에서 빠졌을 수 있고, 그
    사람에게 파일명을 보내면 그 자체가 유출이다(원칙 3 — 막는 쪽이 기본값).

    확인할 수 없으면 **보내지 않는다.**
    """
    try:
        cursor = ""
        for _ in range(10):  # 대형 채널 방어. 10페이지면 충분하다
            res = client.conversations_members(
                channel=channel_id, limit=1000, cursor=cursor or None
            )
            if user in (res.get("members") or []):
                return True
            cursor = str((res.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return False
        return False
    except Exception as exc:  # noqa: BLE001 - 확인 실패는 통과가 아니다
        log.warning("채널 멤버 확인 실패 ch=%s: %s", channel_id, exc)
        return False


# --- 보낸 기록 (멱등) ---------------------------------------------------------


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise AlertError("DATABASE_URL 이 없습니다")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:
        raise AlertError(str(exc)) from exc


def already_sent(alert: ChannelAlert, user: str) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM conversion_alert_sent
             WHERE workspace = %s AND channel_id = %s
               AND recipient = %s AND dedupe_key = %s
            """,
            (alert.workspace, alert.channel_id, user, alert.dedupe_key),
        )
        return cur.fetchone() is not None


def mark_sent(alert: ChannelAlert, user: str) -> None:
    """보낸 것을 남긴다. **파일명·본문은 넣지 않는다** — 개수와 좌표뿐이다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversion_alert_sent
                (workspace, channel_id, recipient, dedupe_key, file_count, sent_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (workspace, channel_id, recipient, dedupe_key) DO NOTHING
            """,
            (
                alert.workspace, alert.channel_id, user, alert.dedupe_key,
                len(alert.items), datetime.now(UTC),
            ),
        )
        conn.commit()


# --- 발송 --------------------------------------------------------------------


def send(client, alert: ChannelAlert, user: str) -> None:
    opened = client.conversations_open(users=user)
    channel = (opened.get("channel") or {}).get("id")
    if not channel:
        raise AlertError("DM 채널을 열지 못했습니다.")
    client.chat_postMessage(channel=channel, text=alert.text())


def run(clients: dict, *, archive_dir, channel_names=None, owner_of=None,
        queue_state=None) -> RunResult:
    """알릴 것을 찾아 보낸다.

    **알림 실패가 변환 큐 상태를 되돌리지 않는다.** 여기서 터져도 그 파일은
    여전히 `failed` 이고, 다음 실행에서 다시 시도한다.
    """
    result = RunResult()
    names = channel_names or {}
    for alert in collect(archive_dir, queue_state=queue_state):
        client = clients.get(alert.workspace)
        if client is None:
            result.skipped += 1
            continue
        named = ChannelAlert(
            workspace=alert.workspace,
            channel_id=alert.channel_id,
            channel_name=names.get((alert.workspace, alert.channel_id), ""),
            items=alert.items,
            pipeline_version=alert.pipeline_version,
        )
        users = recipients(alert.workspace, alert.channel_id, owner_of=owner_of)
        if not users:
            result.no_recipient += 1
            continue
        result.channels.append(f"{alert.workspace}/{alert.channel_id}")
        for user in users:
            try:
                if already_sent(named, user):
                    result.skipped += 1
                    continue
                if not may_see_channel(client, alert.channel_id, user):
                    log.info(
                        "채널 접근 권한이 없어 보내지 않는다 ws=%s ch=%s",
                        alert.workspace, alert.channel_id,
                    )
                    result.skipped += 1
                    continue
                send(client, named, user)
                mark_sent(named, user)
                result.sent += 1
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 나머지를 막지 않는다
                log.warning("알림 발송 실패 %s: %s", named.log_line(), exc)
                result.failed += 1
        log.info("%s", named.log_line())
    return result
