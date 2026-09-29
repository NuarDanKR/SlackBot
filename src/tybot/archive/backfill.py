"""소급 수집 — **복구 수단이지 실시간 수집의 대체가 아니다.**

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §4
상태 규칙: `supervisor_state` · 쓰기: 실시간과 **같은** writer 를 쓴다.

## 무엇을 되찾을 수 있나

Slack 에 **지금 남아 있는 것**만이다.

| 되찾는다 | 못 되찾는다 |
|---|---|
| 사람 메시지·thread reply | 실시간으로 못 받은 **수정 전 본문** |
| 지금 접근되는 첨부·permalink | 이미 삭제돼 history 에 없는 본문 |
| 지금 시점의 수정된 본문 | edit/delete 가 일어난 **정확한 순서** |

그래서 결과를 **「완전 복구」 라고 부르지 않는다**(§4.1). 그렇게 부르면 사람이
그 기간을 다시 안 본다.

## 왜 읽는 것과 쓰는 것을 나누나

`plan()` 은 Slack 만 읽고 `run()` 이 쓴다. dry-run 은 `plan()` 에서 끝나므로
**본문이 화면·로그로 나가지 않는다** — 건수·채널·기간·거부 사유만 나간다.

## cursor 는 쓰기 뒤에만 전진한다

읽자마자 옮기면 그 사이 실패한 구간이 영영 안 메워진다. 다음 실행은 이미 지난
곳부터 읽기 때문이다. 그래서 `run()` 은 **durable write 와 ACK 가 끝난 뒤에만**
`advance_cursor` 를 부른다(`supervisor_state`).

재실행은 겹치는 구간을 다시 읽고 `(workspace, channel_id, message_ts)` 로 거른다.
같은 것을 두 번 쓰지 않는 것은 writer 의 dedupe 계약이기도 하다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .supervisor_state import JobCounts, JobState, advance_cursor, finish_state

log = logging.getLogger("tybot.archive.backfill")

#: 한 번에 받아 오는 메시지 수. Slack 상한은 1000 이지만 크게 잡으면 한 번 실패에
#: 잃는 것이 커진다.
PAGE_SIZE = 200

#: `Retry-After` 가 없을 때 기다리는 기본 초.
DEFAULT_RETRY_SECONDS = 30

#: 스레드 부모를 찾으러 **범위보다 얼마나 더 거슬러 읽나**(초).
#:
#: `conversations.history(oldest=T0)` 는 T0 이후 메시지만 준다. 그런데 T0 **전에**
#: 올라온 글에 T0 이후 답글이 달릴 수 있다. 부모를 못 찾으면 그 답글은 영영 안
#: 들어온다 — 채널에는 보이는데 아카이브에는 없는 상태다.
#:
#: 그래서 부모를 찾을 때만 더 거슬러 읽는다. 그 구간의 **본문은 수집하지 않는다** —
#: 범위 밖이기 때문이다. 읽는 목적은 「이 스레드가 있다」 를 아는 것뿐이다.
#:
#: 기본 14일. 무한히 거슬러 올라가면 오래된 채널에서 한 번 실행이 며칠이 된다.
PARENT_LOOKBACK_SECONDS = 14 * 24 * 60 * 60

#: 수집하지 않는 메시지. 실시간 경로(`archiving_bot.ingest_event`)와 **같은 규칙**이다.
#: 다르면 같은 대화가 경로에 따라 다르게 남는다.
SKIPPED_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "bot_message",
    "message_deleted", "tombstone",
})


@dataclass(frozen=True)
class Target:
    """소급을 돌릴 채널 하나."""

    workspace: str
    channel_id: str
    channel: str = ""
    cursor: str = ""


@dataclass
class Found:
    """읽어 온 사람 메시지 하나. **본문은 여기서 끝난다** — 밖으로 안 나간다."""

    channel_id: str
    ts: str
    payload: dict
    thread_ts: str = ""


@dataclass(frozen=True)
class CollectMeta:
    """한 채널을 **어디까지 훑었나.** 건수와 달리 범위에 대한 사실이다."""

    scanned_from_ts: str = ""
    orphan_replies: int = 0


@dataclass
class Preview:
    """dry-run 결과. **건수와 좌표뿐이다.**"""

    workspace: str
    channels: list[str] = field(default_factory=list)
    found: int = 0
    with_attachment: int = 0
    threads: int = 0
    skipped_bot: int = 0
    skipped_subtype: int = 0
    denied: list[str] = field(default_factory=list)
    exhausted: bool = True
    from_ts: str = ""
    to_ts: str = ""
    #: 스레드 부모를 찾으려고 **실제로 읽은** 가장 이른 지점. 요청한 `from_ts` 보다
    #: 앞설 수 있다. 「무엇을 근거로 이만큼 찾았나」 를 job 결과가 들고 있어야
    #: 사람이 빠진 것을 가늠할 수 있다(§4.1).
    scanned_from_ts: str = ""
    #: 범위 밖 부모에 달린, 범위 안 답글. 0 이 아니면 그 스레드는 부모 없이 들어온다.
    orphan_replies: int = 0

    def as_json(self) -> dict:
        return {
            "workspace": self.workspace,
            "channels": list(self.channels),
            "found": self.found,
            "withAttachment": self.with_attachment,
            "threads": self.threads,
            "skippedBot": self.skipped_bot,
            "skippedSubtype": self.skipped_subtype,
            "denied": list(self.denied),
            "exhausted": self.exhausted,
            "fromTs": self.from_ts,
            "toTs": self.to_ts,
            "scannedFromTs": self.scanned_from_ts,
            "orphanReplies": self.orphan_replies,
            # 「완전 복구」 라고 부르지 않는다(§4.1). 그렇게 부르면 사람이 그 기간을
            # 다시 안 본다.
            "note": (
                "지금 Slack 에 남아 있는 것만 셉니다. 수정 전 본문과 이미 삭제된"
                " 메시지는 되찾을 수 없습니다."
                + (
                    f" 스레드 부모를 찾으려고 {self.scanned_from_ts} 까지 거슬러"
                    " 읽었고, 그보다 오래된 부모의 답글은 찾지 못합니다."
                    if self.scanned_from_ts else ""
                )
            ),
        }


class SlackDenied(RuntimeError):
    """그 채널을 읽을 수 없다. **어느 채널인지만** 들고 있다."""

    def __init__(self, channel_id: str, code: str) -> None:
        super().__init__(f"{channel_id}: {code}")
        self.channel_id = channel_id
        self.code = code


def _error_code(exc: Exception) -> str:
    """Slack 오류를 **코드로만** 옮긴다. 본문·토큰이 섞여 나가지 않게."""
    response = getattr(exc, "response", None)
    data = getattr(response, "data", None)
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return type(exc).__name__


def _retry_seconds(exc: Exception) -> float:
    """Slack 이 기다리라고 한 시간. 없으면 기본값."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    with_header = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(with_header)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_SECONDS


def _is_human(message: dict) -> tuple[bool, str]:
    """사람이 쓴 것인가. 아니면 **왜 아닌지** 돌려준다.

    실시간 경로와 같은 규칙이다 — 봇·앱·시스템 메시지와 DM 은 안 모은다.
    """
    if message.get("bot_id") or message.get("app_id"):
        return False, "bot"
    subtype = str(message.get("subtype") or "")
    if subtype in SKIPPED_SUBTYPES:
        return False, "subtype"
    if subtype and subtype not in ("file_share", "thread_broadcast"):
        return False, "subtype"
    if not message.get("user") or not message.get("ts"):
        return False, "subtype"
    return True, ""


def _in_range(ts: str, *, oldest: str, latest: str) -> bool:
    """Slack ts가 요청 범위 안인가.

    thread API는 부모를 기준으로 호출하므로 요청한 `latest` 뒤의 답글도 돌려줄 수
    있다. API 응답 모양에 기대지 않고 쓰기 직전에 한 번 더 막는다.
    """
    if oldest and ts < oldest:
        return False
    return not (latest and ts > latest)


def _pages(
    call: Callable[..., dict], *, sleeper: Callable[[float], None], **kwargs,
) -> Iterator[dict]:
    """cursor 를 끝까지 넘긴다. **429 는 기다렸다 다시 부른다.**

    한 페이지만 읽고 끝내면 오래된 채널에서 앞부분만 메워지고, 그건 「했다」 로
    보이는 실패다.
    """
    cursor = ""
    while True:
        try:
            response = call(**kwargs, limit=PAGE_SIZE, **({"cursor": cursor} if cursor else {}))
        except Exception as exc:
            code = _error_code(exc)
            if code in ("ratelimited", "rate_limited"):
                sleeper(_retry_seconds(exc))
                continue
            raise SlackDenied(str(kwargs.get("channel", "")), code) from exc
        yield response
        cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return


def parent_scan_oldest(oldest: str, *, lookback: float = PARENT_LOOKBACK_SECONDS) -> str:
    """부모를 찾으러 읽기 시작할 지점. 범위 시작보다 **더 거슬러 간다.**

    범위 시작이 없으면(채널 전체) 거슬러 갈 것도 없다.
    """
    if not oldest:
        return ""
    try:
        return f"{max(0.0, float(oldest) - lookback):.6f}"
    except ValueError:
        return oldest


def collect(
    client,
    target: Target,
    *,
    oldest: str = "",
    latest: str = "",
    sleeper: Callable[[float], None] = time.sleep,
    lookback: float = PARENT_LOOKBACK_SECONDS,
) -> tuple[list[Found], bool, CollectMeta]:
    """한 채널의 사람 메시지와 thread reply 를 모은다.

    `(결과, 끝까지 읽었나, 어디까지 훑었나)`.

    ## 범위 밖 부모

    `conversations.history(oldest=T0)` 는 T0 이후만 준다. **T0 전에 올라온 글에
    T0 이후 답글이 달린 경우**, 부모를 못 찾으면 그 답글은 영영 안 들어온다 —
    채널에는 보이는데 아카이브에는 없다.

    그래서 부모를 찾을 때만 `lookback` 만큼 더 거슬러 읽는다. 그 구간의 **본문은
    수집하지 않는다**(범위 밖이다). 읽는 목적은 스레드의 존재를 아는 것뿐이고,
    얼마나 거슬렀는지는 결과에 적는다 — 그보다 오래된 부모는 여전히 못 찾는다.
    """
    found: list[Found] = []
    seen: set[str] = set()
    threads: set[str] = set()
    exhausted = True
    scan_from = parent_scan_oldest(oldest, lookback=lookback)
    orphan_replies = 0

    for page in _pages(
        client.conversations_history, sleeper=sleeper,
        channel=target.channel_id,
        **({"oldest": scan_from} if scan_from else {}),
        **({"latest": latest} if latest else {}),
    ):
        for message in page.get("messages") or []:
            ts = str(message.get("ts") or "")
            human, _ = _is_human(message)
            # conversations.history의 thread 부모는 보통 thread_ts가 없고
            # reply_count/latest_reply만 가진다. thread_ts만 보면 답글이 빠진다.
            is_parent = (
                bool(ts)
                and (
                    (message.get("thread_ts") and str(message["thread_ts"]) == ts)
                    or bool(message.get("reply_count"))
                    or bool(message.get("latest_reply"))
                )
            )
            if is_parent:
                threads.add(ts)
            if not human or not ts or ts in seen or not _in_range(
                ts, oldest=oldest, latest=latest
            ):
                continue
            seen.add(ts)
            found.append(Found(target.channel_id, ts, message))

    for thread_ts in sorted(threads):
        outside = bool(oldest) and thread_ts < oldest
        picked = 0
        for page in _pages(
            client.conversations_replies, sleeper=sleeper,
            channel=target.channel_id, ts=thread_ts,
        ):
            for message in page.get("messages") or []:
                ts = str(message.get("ts") or "")
                human, _ = _is_human(message)
                if not human or not ts or ts in seen or not _in_range(
                    ts, oldest=oldest, latest=latest
                ):
                    continue
                seen.add(ts)
                picked += 1
                found.append(Found(target.channel_id, ts, message, thread_ts=thread_ts))
        if outside and picked:
            # 부모는 범위 밖이라 안 들어온다. 답글만 들어오는 스레드가 몇 개인지
            # 세어 둔다 — 「부분」 이라는 사실을 결과가 말할 수 있어야 한다.
            orphan_replies += 1

    found.sort(key=lambda item: item.ts)
    return found, exhausted, CollectMeta(scanned_from_ts=scan_from, orphan_replies=orphan_replies)


def plan(
    client,
    targets: list[Target],
    *,
    workspace: str,
    oldest: str = "",
    latest: str = "",
    sleeper: Callable[[float], None] = time.sleep,
) -> Preview:
    """dry-run. **읽기만 한다.**

    본문을 반환값에 넣지 않는다 — 콘솔과 로그에 원문이 흐르면 그때부터 원문이
    아카이브 밖에도 있게 된다.
    """
    preview = Preview(workspace=workspace, from_ts=oldest, to_ts=latest)
    for target in targets:
        try:
            found, exhausted, meta = collect(
                client, target, oldest=oldest, latest=latest, sleeper=sleeper,
            )
        except SlackDenied as denied:
            # 권한이 없는 채널은 **건수 0 이 아니라 거부**다. 0 으로 세면 「없었다」
            # 로 읽히고, 사람은 그 채널을 다시 안 본다.
            preview.denied.append(f"{denied.channel_id}: {denied.code}")
            preview.exhausted = False
            continue
        preview.channels.append(target.channel_id)
        preview.found += len(found)
        preview.threads += len({item.thread_ts for item in found if item.thread_ts})
        preview.with_attachment += sum(
            1 for item in found if item.payload.get("files")
        )
        preview.exhausted = preview.exhausted and exhausted
        preview.scanned_from_ts = meta.scanned_from_ts
        preview.orphan_replies += meta.orphan_replies
    return preview


def run(
    client,
    targets: list[Target],
    *,
    workspace: str,
    ingest: Callable[[Target, Found], str],
    save_cursor: Callable[[Target, str], None],
    oldest: str = "",
    latest: str = "",
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[JobCounts, JobState]:
    """실제 소급. **쓰는 일은 `ingest` 가 한다** — 실시간과 같은 writer 다.

    `ingest` 는 결과를 문자열로 돌려준다.

    | 값 | 뜻 |
    |---|---|
    | `written` | 새로 디스크에 들어갔다 |
    | `duplicate` | 이미 있던 것이다(멱등) |
    | `refused` | PII 등으로 거절됐다 |
    | 그 외 | 실패로 센다 |

    cursor 는 **그 채널의 쓰기가 전부 끝난 뒤** 한 번만 옮긴다. 메시지마다 옮기면
    중간에 끊겼을 때 안 쓴 구간이 지난 것으로 남는다.
    """
    counts = JobCounts()
    exhausted = True

    for target in targets:
        try:
            found, channel_exhausted, _meta = collect(
                client, target, oldest=oldest, latest=latest, sleeper=sleeper,
            )
        except SlackDenied as denied:
            log.warning("소급 대상 채널을 읽지 못했다 ch=%s code=%s",
                        denied.channel_id, denied.code)
            counts = JobCounts(
                counts.found, counts.written, counts.duplicate,
                counts.refused, counts.failed + 1,
            )
            exhausted = False
            continue

        written = duplicate = refused = failed = 0
        highest = ""
        for item in found:
            try:
                result = ingest(target, item)
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 채널을 멈추면 안 된다
                log.warning("소급 기록 실패 ch=%s ts=%s code=%s",
                            target.channel_id, item.ts, type(exc).__name__)
                failed += 1
                # **여기서 멈춘다.** 뒤 메시지를 계속 쓰면 cursor 를 어디까지
                # 옮겨야 하는지 알 수 없다.
                exhausted = False
                break
            if result == "written":
                written += 1
            elif result == "duplicate":
                duplicate += 1
            elif result == "refused":
                refused += 1
            else:
                failed += 1
                exhausted = False
                break
            highest = item.ts

        counts = JobCounts(
            found=counts.found + len(found),
            written=counts.written + written,
            duplicate=counts.duplicate + duplicate,
            refused=counts.refused + refused,
            failed=counts.failed + failed,
        )
        exhausted = exhausted and channel_exhausted
        # 실패가 없을 때만 전진한다. 실패 뒤에 멈춘 지점까지 옮기면 그 뒤 구간이
        # 「읽은 것」 으로 남는다.
        moved = advance_cursor(target.cursor, highest, durable=not failed and bool(highest))
        if moved != target.cursor:
            save_cursor(target, moved)

    return counts, finish_state(counts, exhausted=exhausted)
