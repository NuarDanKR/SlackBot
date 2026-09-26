"""수집 ACK — 「방금 올린 것이 **운영 검색**에 잡히나」 에 답하는 유일한 근거.

결정: 2026-09-25 오너 §6, 2026-09-26 보완 지시 1~6.
스키마: `archive_ingest_state` (`deploy/sql/archiving_schema.sql`).

## 왜 필요한가

본문은 즉시 쓰이고 첨부 변환은 큐를 지난다. 그래서 「올렸습니다」 와 「검색됩니다」
사이에 **시간이 있다.** 그 사이를 모른 채 답하면 두 방향으로 틀린다 — 본문은 이미
있는데 「아직」 이라고 하거나, 첨부가 아직인데 「됐다」 고 한다.

뒤쪽이 더 나쁘다. 사람이 찾으러 갔다가 못 찾으면 **봇이 거짓말한 것**이 되고,
그 다음부터는 맞는 답도 확인하러 간다.

## `ready` 만으로는 검색된다고 말할 수 없다

그림자 수집도 `ready` 가 된다 — 그림자 경로에서는 본문도 첨부도 다 끝났기
때문이다. 그런데 그 파일은 **운영 아카이브에 없다.** 운영 검색은 못 찾는다.

그래서 단언에는 두 값이 다 필요하다: `state == ready` **그리고**
`written_to == live`. 처음 구현은 `state` 만 봐서, 그림자 수집을 운영 검색
가능으로 보고했다(2026-09-26 지적). 이 파일이 막으려던 바로 그 거짓말이다.

## 앞으로만 간다

Slack 은 재전달을 한다. `ready` 인 메시지에 `received` 가 다시 오면 그것은 새
사실이 아니라 **같은 사실의 재방송**이다. 뒤로 보내면 그 순간 「검색된다」 가
「아직」 으로 바뀌고, 사람은 방금 본 것이 사라졌다고 읽는다.

판정은 `archiving_state.plan_ingest_change` 가 한다. 여기서 규칙을 다시 쓰지
않는다 — 두 곳에 있으면 한 곳만 고치는 날이 온다.

## DB 가 죽어도 사실은 남는다

ACK 를 못 쓰면 상태가 통째로 사라진다. 수집은 됐는데 기록이 없으니, 복구 뒤에도
「모른다」 가 되고 사람은 다시 올린다. 그래서 **DB 에 못 쓰면 파일에 쓴다**
(`outbox`). 복구되면 그대로 다시 민다 — `advance` 가 멱등이라 두 번 밀어도 같다.

outbox 는 `STATE_DIR` 아래에 둔다. 아카이브 밖이라 `ArchiveStore` 글롭에 안 걸리고,
따라서 답변 근거가 되지 않는다.

## 모르면 「된다」 고 하지 않는다

행이 없거나 DB 를 못 보면 **검색 가능하다고 말하지 않는다.** 없는 것을 「아직
안 됐다」 로도 말하지 않는다 — 그것도 모르면서 아는 척이다.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..lock import AlreadyRunning, FileLock, LockUnavailable
from .archiving_state import (
    IngestProgress,
    IngestState,
    TransitionRefused,
    searchable_claim,
)

log = logging.getLogger("tybot.archive.ingest_ack")

#: 어디에 썼나. 빈 값은 **모른다** 다.
LIVE = "live"
SHADOW = "shadow"

#: 상태를 못 봤을 때 하는 말. **검색 가능 여부를 말하지 않는다.**
UNKNOWN_CLAIM = "수집 상태를 확인하지 못했습니다. 검색 가능 여부는 말씀드릴 수 없습니다."

#: 그림자에만 있는 것. 「기록됐다」 와 「검색된다」 를 한 문장에서 갈라 준다.
SHADOW_CLAIM = "그림자 수집에는 기록됐지만 운영 검색에는 아직 반영되지 않았습니다"


@dataclass(frozen=True)
class AckStatus:
    """지금 상태와 **어디에 썼는지.** 둘을 따로 들어야 거짓말을 막는다."""

    progress: IngestProgress
    written_to: str = ""

    @property
    def state(self) -> IngestState:
        return self.progress.state

    @property
    def searchable(self) -> bool:
        """운영 검색이 찾을 수 있다고 **단언**해도 되는가.

        `ready` 하나로는 부족하다 — 그림자도 `ready` 가 된다. 운영 아카이브에
        쓰인 것(`live`)만 참이다.
        """
        return self.state == IngestState.READY and self.written_to == LIVE


def enabled() -> bool:
    """DB 가 없으면 DB 에 기록하지 않는다(대신 outbox 로 간다).

    기록을 못 한다고 수집을 멈추면 **놓친 원본은 되돌릴 수 없다** — Slack 백필은
    분당 1요청이라 사실상 복구가 안 된다. 반대로 ACK 는 나중에 다시 만들 수 있다.
    """
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    from ..console.workspace_store import _connect as connect

    return connect()


# ---------------------------------------------------------------------------
# outbox — DB 가 죽어도 사실은 남는다
# ---------------------------------------------------------------------------


def outbox_path(workspace: str) -> Path:
    """`STATE_DIR` 아래. **아카이브 밖이라 답변 근거가 되지 않는다.**"""
    base = Path(os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot")
    return base / "state" / "ingest-ack-outbox" / f"{workspace}.jsonl"


def dead_letter_path(workspace: str) -> Path:
    """파싱할 수 없는 행을 보존하는 감사 경로."""
    return outbox_path(workspace).with_suffix(".bad.jsonl")


def _outbox_lock(path: Path) -> FileLock:
    return FileLock(path.with_suffix(".lock"), label=f"ACK outbox {path.stem}")


def _spool(payload: dict) -> bool:
    """DB 에 못 쓴 것을 파일에 쌓는다. 파일에도 못 쓰면 `False`."""
    path = outbox_path(str(payload.get("workspace") or "unknown"))
    lock = _outbox_lock(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock.acquire(timeout=20)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return True
    except (AlreadyRunning, LockUnavailable, OSError):
        log.exception("outbox 에 ACK 를 쓰지 못했다 path=%s", path)
        return False
    finally:
        lock.release()


def drain_outbox(workspace: str) -> dict:
    """복구 뒤 다시 민다. **멱등이라 두 번 밀어도 같다.**

    성공한 줄만 지운다 — 통째로 지우면 한 줄이 실패했을 때 나머지 사실도 같이
    사라진다. 남은 줄은 다음 회차가 다시 시도한다.
    """
    path = outbox_path(workspace)
    if not enabled() or not path.is_file():
        return {"applied": 0, "left": 0}

    lock = _outbox_lock(path)
    try:
        # append 와 정리를 같은 잠금으로 직렬화한다. 그렇지 않으면 read_text 뒤에
        # 들어온 새 ACK 를 아래 write_text/unlink 가 지울 수 있다.
        lock.acquire(timeout=20)
        lines = path.read_text(encoding="utf-8").splitlines()
    except (AlreadyRunning, LockUnavailable, OSError):
        log.exception("outbox 를 읽지 못했다 ws=%s", workspace)
        return {"applied": 0, "left": 0}

    try:
        applied, leftover, corrupt = 0, [], []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                # 재시도를 막지는 않되 감사 사실을 없애지도 않는다.
                log.error("outbox 에 깨진 줄이 있어 격리한다 ws=%s", workspace)
                corrupt.append(line)
                continue
            if _write(payload) is None:
                leftover.append(line)
            else:
                applied += 1

        if corrupt:
            bad = dead_letter_path(workspace)
            with bad.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(corrupt) + "\n")
        if leftover:
            replacement = path.with_suffix(".tmp")
            replacement.write_text(
                "\n".join(leftover) + "\n", encoding="utf-8", newline="\n"
            )
            replacement.replace(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        log.exception("outbox 를 정리하지 못했다 ws=%s", workspace)
    finally:
        lock.release()
    return {"applied": applied, "left": len(leftover)}


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------


def read(workspace: str, channel_id: str, message_ts: str) -> AckStatus | None:
    """지금 상태. 행이 없거나 못 보면 `None` — **「아직」 과 구분한다.**"""
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT state, attachment_total, attachment_ready, written_to
                  FROM archive_ingest_state
                 WHERE workspace = %s AND channel_id = %s AND message_ts = %s
                """,
                (workspace, channel_id, message_ts),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - 못 보는 것과 없는 것을 같은 값으로 돌려준다
        log.warning("수집 상태를 읽지 못했다 ws=%s ch=%s", workspace, channel_id)
        return None
    if row is None:
        return None
    return AckStatus(
        IngestProgress(
            IngestState(str(row["state"])),
            attachment_total=int(row["attachment_total"] or 0),
            attachment_ready=int(row["attachment_ready"] or 0),
        ),
        written_to=str(row["written_to"] or ""),
    )


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------


def advance(
    *,
    workspace: str,
    channel_id: str,
    message_ts: str,
    target: IngestState,
    attachment_total: int | None = None,
    attachment_ready: int | None = None,
    written_to: str = "",
    doc_path: str = "",
    error_code: str = "",
) -> IngestState | None:
    """상태를 앞으로 민다. 못 가면 **지금 상태를 그대로** 돌려준다.

    예외를 던지지 않는다. 재전달은 오류가 아니라 정상이다 — 던지면 수집 루프가
    그때마다 예외를 먹는다.

    DB 에 못 쓰면 outbox 에 쌓고 `None` 을 돌려준다. `None` 은 **지금은 기록되지
    않았다** 는 뜻이고, 「상태가 없다」 와 다르다.
    """
    payload = {
        "workspace": workspace,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "target": str(target),
        "attachment_total": attachment_total,
        "attachment_ready": attachment_ready,
        "written_to": written_to,
        "doc_path": doc_path,
        "error_code": error_code,
        "queued_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if enabled():
        got = _write(payload)
        if got is not None:
            # DB 가 다시 살아난 첫 성공에서 장애 중 쌓인 사실도 함께 복구한다.
            # drain 은 `_write` 를 직접 호출하므로 재귀하지 않는다.
            if outbox_path(workspace).is_file():
                drained = drain_outbox(workspace)
                if drained["applied"]:
                    log.info(
                        "ACK outbox 복구 ws=%s applied=%s left=%s",
                        workspace, drained["applied"], drained["left"],
                    )
            return got
    # 여기 왔다는 것은 DB 가 없거나 못 썼다는 뜻이다. 사실을 잃지 않는다.
    if not _spool(payload):
        # 파일에도 못 썼다. **운영이 알아야 한다** — 이 시점부터 수집은 되는데
        # 상태는 통째로 없다. 그 상태에서 사람이 물으면 봇은 「모른다」 만 한다.
        log.error(
            "수집 상태를 DB 에도 outbox 에도 기록하지 못했다 ws=%s ch=%s ts=%s "
            "— 이 메시지의 ACK 가 없다",
            workspace, channel_id, message_ts,
        )
    return None


def _write(payload: dict) -> IngestState | None:
    """실제 DB 쓰기 한 번. 실패하면 `None`."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            # 같은 메시지에 두 경로가 동시에 닿을 수 있다(실시간·백필·재전달).
            # 좌표별로 잠가야 「읽고 판정하고 쓰는」 사이가 벌어지지 않는다.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (
                    f"{payload['workspace']}\x1f{payload['channel_id']}",
                    payload["message_ts"],
                ),
            )
            cur.execute(
                """
                SELECT state, attachment_total, attachment_ready, written_to
                  FROM archive_ingest_state
                 WHERE workspace = %s AND channel_id = %s AND message_ts = %s
                 FOR UPDATE
                """,
                (payload["workspace"], payload["channel_id"], payload["message_ts"]),
            )
            row = cur.fetchone()
            current = (
                AckStatus(
                    IngestProgress(
                        IngestState(str(row["state"])),
                        int(row["attachment_total"] or 0),
                        int(row["attachment_ready"] or 0),
                    ),
                    str(row["written_to"] or ""),
                )
                if row
                else None
            )
            plan = _next(
                current,
                IngestState(str(payload["target"])),
                payload.get("attachment_total"),
                payload.get("attachment_ready"),
            )
            if plan is None:
                return current.state if current else None

            cur.execute(
                """
                INSERT INTO archive_ingest_state
                    (workspace, channel_id, message_ts, state,
                     attachment_total, attachment_ready, written_to, doc_path, error_code)
                VALUES (%(workspace)s, %(channel_id)s, %(message_ts)s, %(state)s,
                        %(total)s, %(ready)s, %(written_to)s, %(doc_path)s, %(error_code)s)
                ON CONFLICT (workspace, channel_id, message_ts) DO UPDATE SET
                    state = excluded.state,
                    attachment_total = excluded.attachment_total,
                    attachment_ready = excluded.attachment_ready,
                    written_to = CASE WHEN excluded.written_to <> '' THEN excluded.written_to
                                      ELSE archive_ingest_state.written_to END,
                    doc_path = CASE WHEN excluded.doc_path <> '' THEN excluded.doc_path
                                    ELSE archive_ingest_state.doc_path END,
                    error_code = excluded.error_code,
                    updated_at = now()
                """,
                {
                    "workspace": payload["workspace"],
                    "channel_id": payload["channel_id"],
                    "message_ts": payload["message_ts"],
                    "state": str(plan.state),
                    "total": plan.attachment_total,
                    "ready": plan.attachment_ready,
                    "written_to": payload.get("written_to") or "",
                    "doc_path": payload.get("doc_path") or "",
                    "error_code": payload.get("error_code") or "",
                },
            )
            return plan.state
    except Exception:  # noqa: BLE001 - 어떤 DB 오류든 outbox 로 넘긴다
        log.warning(
            "수집 상태를 DB 에 기록하지 못했다 ws=%s ch=%s — outbox 로 넘긴다",
            payload.get("workspace"), payload.get("channel_id"),
        )
        return None


def _next(
    current: AckStatus | None,
    target: IngestState,
    attachment_total: int | None,
    attachment_ready: int | None,
) -> IngestProgress | None:
    """다음 상태. **갈 수 없으면 `None`** (재전달이므로 조용히 둔다).

    첨부 수는 「준 것만」 바꾼다. 안 주면 지금 값을 유지한다 — 본문 경로가 첨부
    수를 0 으로 덮으면, 첨부가 아직인데 `ready` 로 갈 수 있게 된다.
    """
    base = current.progress if current else IngestProgress(IngestState.RECEIVED)
    total = base.attachment_total if attachment_total is None else attachment_total
    ready = base.attachment_ready if attachment_ready is None else attachment_ready
    counted = IngestProgress(base.state, total, max(0, min(ready, total)))

    if current is None:
        return IngestProgress(target, counted.attachment_total, counted.attachment_ready)
    if target == counted.state:
        # 같은 상태로 다시 왔다. 첨부 수만 늘 수 있다(변환이 하나 더 끝난 경우).
        return counted if counted != current.progress else None
    try:
        from .archiving_state import plan_ingest_change

        return plan_ingest_change(counted, target)
    except TransitionRefused:
        return None


# ---------------------------------------------------------------------------
# Master 가 답하기 전에 부르는 자리
# ---------------------------------------------------------------------------


def claim(
    workspace: str, channel_id: str, message_ts: str, *, require_ack: bool = True
) -> str:
    """사람에게 할 말. **여기를 거치지 않고 「검색 가능」 을 말하지 않는다.**

    문구를 한 자리에서 만드는 이유: 호출부마다 쓰면 한 군데가 「올렸습니다」 를
    「검색됩니다」 로 적고, 그게 제일 안 들킨다.
    """
    return claim_for(read(workspace, channel_id, message_ts), require_ack=require_ack)


def claim_for(status: AckStatus | None, *, require_ack: bool = True) -> str:
    """상태 하나를 문장으로. 읽기와 갈라 두어 시험이 DB 없이 전부 본다."""
    if status is None:
        return UNKNOWN_CLAIM
    if status.written_to != LIVE:
        # ready 전 단계도 그림자 기록이다. 일반 진행 문구만 내면 운영 아카이브에
        # 원문이 들어간 것으로 오해할 수 있으므로 목적지를 먼저 밝힌다.
        return SHADOW_CLAIM
    return searchable_claim(status.progress, require_ack=require_ack)


def is_searchable(workspace: str, channel_id: str, message_ts: str) -> bool:
    """운영 검색이 찾는다고 단언해도 되는가. **모르면 `False`.**"""
    status = read(workspace, channel_id, message_ts)
    return status is not None and status.searchable
