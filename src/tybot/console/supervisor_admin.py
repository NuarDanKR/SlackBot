"""Archiver supervisor 운영 손잡이 — **콘솔에서만 돌린다.**

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §3.2·§3.3·§4
순수 전이: `tybot.archive.supervisor_state` · SQL: `supervisor_repo`

## 이 모듈이 지키는 네 규칙

**1. 저장은 기록이다.** 콘솔은 `desired_mode` 와 generation 만 쓴다. 실제로 멈추고
시작하는 것은 supervisor 이고, 그 사이에는 시간이 있다. 화면이 「적용됐다」 로 보이면
사람은 확인하지 않고 떠나고, 안 멈춘 worker 를 아무도 안 본다. 그래서 읽기 응답마다
`runtimeEffect` 를 같이 낸다.

**2. 사람과 사유 없이는 못 바꾼다.** 소급 수집은 되돌릴 수 없다 — 누가 왜 걸었는지
없으면 사고가 났을 때 범위를 정할 수 없다.

**3. 세어 보기 전에는 못 건다.** 실제 소급은 같은 범위의 dry-run 이 끝난 뒤에만
받는다(CLAUDE.md 「파괴적이거나 비용이 드는 동작은 범위를 명시하게 만든다」).

**4. 콘솔은 Slack 도 shell 도 부르지 않는다.** 여기서 만드는 것은 **줄 세운 요청**
이고, supervisor 가 집어간다(§8).

## SQL 은 여기 없다

`supervisor_repo` 가 든다. 그래서 시험은 가짜 저장소 하나로 규칙을 전부 본다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..archive.supervisor_state import (
    DesiredMode,
    ObservedState,
    TransitionRefused,
    WorkspaceRuntime,
    plan_desired_change,
    stale,
)
from .archiving_admin import Actor, AdminRefused
from .supervisor_repo import RECENT_JOBS, SupervisorRepo, default_repo

#: 감사에 남길 때 쓰는 주어.
SUBJECT_DESIRED = "archiver_desired_mode"
SUBJECT_BACKFILL = "archiver_backfill"

#: Slack ts 의 모양. 이것 말고는 받지 않는다 — 사람이 적은 날짜를 그대로 넘기면
#: Slack 이 조용히 0 으로 읽고, 그러면 채널 전체를 긁는다.
TS = re.compile(r"^\d{1,12}\.\d{1,6}$")

#: 아직 끝나지 않은 작업의 상태. 같은 채널에 겹쳐 걸지 않는다.
OPEN_JOB_STATES = frozenset({"queued", "running"})

#: 저장이 곧 적용이 아니라는 사실을 **모든 읽기 응답이** 들고 다닌다.
RUNTIME_EFFECT = (
    "저장은 기록입니다. supervisor 가 다음 설정 읽기에서 세대를 보고 적용하며,"
    " 그때까지 실제 프로세스 상태는 바뀌지 않습니다."
)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _age(value, now: datetime) -> float | None:
    if not isinstance(value, datetime):
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return (now - moment).total_seconds()


def _runtime_from_row(workspace: str, row: dict | None) -> WorkspaceRuntime:
    if row is None:
        return WorkspaceRuntime(workspace=workspace)
    return WorkspaceRuntime(
        workspace=workspace,
        desired_mode=DesiredMode(str(row["desired_mode"])),
        observed_state=ObservedState(str(row["observed_state"])),
        generation=int(row["generation"]),
        error_code=str(row.get("error_code") or ""),
    )


def health(row: dict | None, *, now: datetime, limit_seconds: float = 180) -> dict:
    """지금 무엇이 고장났나. **desired 와 observed 의 차이가 곧 답이다.**

    | 보이는 것 | 뜻 |
    |---|---|
    | `unknown` | 한 번도 안 떴다. 기동 전이거나 supervisor 가 이 워크스페이스를 모른다 |
    | `off` | 사람이 껐고 실제로 멈춰 있다 |
    | `stopping` | 껐는데 아직 도는 중이다. **과도 상태라 오류가 아니다** |
    | `starting` | 켰는데 아직 안 떴다 |
    | `ok` | 원하는 대로 돌고 heartbeat 도 살아 있다 |
    | `degraded` | 돌지만 온전치 않다. heartbeat 유실도 여기다 |
    | `error` | 멈췄고 사유가 있다 |

    heartbeat 유실을 `ok` 로 두지 않는 이유: 프로세스가 죽어도 마지막 관측 상태는
    `running` 으로 남는다. 그 값만 보면 화면은 계속 정상이라고 말한다.
    """
    if row is None:
        return {
            "state": "unknown", "reason": "supervisor 가 아직 이 워크스페이스를 보고한 적이 없습니다.",
            "heartbeatAgeSeconds": None, "heartbeatStale": False,
        }
    desired = str(row["desired_mode"])
    observed = str(row["observed_state"])
    age = _age(row.get("heartbeat_at"), now)
    lost = desired != DesiredMode.OFF and stale(age, limit_seconds=limit_seconds)

    if observed == ObservedState.ERROR:
        state, reason = "error", "worker 가 멈췄습니다."
    elif desired == DesiredMode.OFF and observed == ObservedState.STOPPED:
        state, reason = "off", "사람이 껐고 실제로 멈춰 있습니다."
    elif desired == DesiredMode.OFF:
        state, reason = "stopping", "끄는 중입니다. worker 가 아직 종료되지 않았습니다."
    elif lost:
        state, reason = (
            "degraded",
            "heartbeat 가 끊겼습니다. 관측 상태는 옛 값이라 그대로 믿을 수 없습니다.",
        )
    elif observed == ObservedState.DEGRADED:
        state, reason = "degraded", "돌고 있지만 일부가 안 됩니다."
    elif observed == ObservedState.RUNNING:
        state, reason = "ok", "원하는 대로 돌고 있습니다."
    else:
        state, reason = "starting", "아직 기동 중입니다."
    return {
        "state": state,
        "reason": reason,
        "heartbeatAgeSeconds": age,
        "heartbeatStale": lost,
    }


def runtime_detail(
    workspace: str,
    repo: SupervisorRepo | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """화면 한 장에 필요한 것을 **한 번에** 모은다.

    따로 질의하면 사람이 보는 순간의 상태가 서로 다른 시각의 것이 된다. 상태는
    바뀌었는데 작업 목록은 아직 옛 것이면, 사람은 자기가 누른 것이 안 먹었다고
    생각해 한 번 더 누른다 — 소급에서는 그 한 번이 같은 범위를 두 번 긁는다.
    """
    store = repo or default_repo()
    moment = _now(now)
    row = store.runtime(workspace)
    jobs = store.jobs(workspace, RECENT_JOBS)
    return {
        "workspace": workspace,
        "desiredMode": str(row["desired_mode"]) if row else str(DesiredMode.OFF),
        "observedState": str(row["observed_state"]) if row else str(ObservedState.STOPPED),
        "generation": int(row["generation"]) if row else 0,
        "heartbeatAt": row.get("heartbeat_at") if row else None,
        "lastEventAt": row.get("last_event_at") if row else None,
        "lastWriteAt": row.get("last_write_at") if row else None,
        "errorCode": str(row.get("error_code") or "") if row else "",
        "errorNote": str(row.get("error_note") or "") if row else "",
        "health": health(row, now=moment),
        "cursors": store.cursors(workspace),
        "jobs": jobs,
        # 공통 root 아래 이 워크스페이스의 자리(§3.3). 절대경로는 서버 env 가 정하고
        # 콘솔은 모른다 — 모르는 값을 화면에 적으면 사람이 그 경로를 찾으러 간다.
        "archiveRelativePath": f"workspaces/{workspace}",
        # 파일럿에서 고를 수 있는 것만. 화면이 버튼을 회색으로 만들 근거다.
        "selectableModes": [str(DesiredMode.OFF), str(DesiredMode.SHADOW)],
        "gatedModes": [str(DesiredMode.LIVE)],
        # 저장이 곧 적용이 아니다. 성공처럼 보이는 표시를 하지 않게 화면에 넘긴다.
        "runtimeEffect": RUNTIME_EFFECT,
    }


def set_desired_mode(
    workspace: str,
    mode: str,
    actor: Actor,
    repo: SupervisorRepo | None = None,
    *,
    audit_repo=None,
) -> dict:
    """사람이 원하는 상태를 적는다. **프로세스를 멈추지는 않는다.**

    `live` 는 거절한다(`plan_desired_change`). 파일럿에서 운영 수집으로 넘어가려면
    release gate·채널별 writer 인수·Master writer 중지가 먼저다.
    """
    store = repo or default_repo()
    with store.transaction() as tx:
        current = _runtime_from_row(workspace, tx.runtime(workspace))
        try:
            planned = plan_desired_change(current, mode)
        except TransitionRefused as exc:
            raise AdminRefused(str(exc)) from exc
        tx.save_desired(
            workspace, str(planned.desired_mode), planned.generation, actor.name,
        )
        _audit(
            audit_repo, actor, SUBJECT_DESIRED, workspace, "",
            field="desired_mode",
            old=str(current.desired_mode), new=str(planned.desired_mode),
        )
    return runtime_detail(workspace, store)


def _check_range(from_ts: str, to_ts: str) -> None:
    for name, value in (("시작", from_ts), ("끝", to_ts)):
        if value and not TS.match(value):
            raise AdminRefused(
                f"{name} 시각이 Slack ts 모양이 아닙니다: {value}."
                " 예: 1759100000.000100"
            )
    if from_ts and to_ts and float(from_ts) >= float(to_ts):
        raise AdminRefused("끝 시각이 시작 시각보다 뒤여야 합니다.")


def _same_scope(job: dict, workspace: str, channel_id: str,
                from_ts: str, to_ts: str) -> bool:
    return (
        str(job["workspace"]) == workspace
        and str(job.get("channel_id") or "") == channel_id
        and str(job.get("from_ts") or "") == from_ts
        and str(job.get("to_ts") or "") == to_ts
    )


def request_backfill(
    workspace: str,
    actor: Actor,
    repo: SupervisorRepo | None = None,
    *,
    channel_id: str = "",
    from_ts: str = "",
    to_ts: str = "",
    dry_run: bool = True,
    audit_repo=None,
) -> dict:
    """소급 수집을 **줄 세운다.** 여기서 Slack 을 읽지 않는다.

    실제 수집은 같은 범위의 dry-run 이 끝난 뒤에만 받는다. 세어 보지 않고 거는
    소급은 분량을 모른 채 rate limit 에 걸리고, 중간에 멈춘 것과 다 된 것을
    구분할 수 없다.

    같은 채널에 아직 끝나지 않은 작업이 있으면 거절한다. 두 작업이 같은 구간을
    동시에 읽으면 cursor 가 서로를 덮고, 그러면 안 메운 구간이 메워진 것으로 남는다.
    """
    store = repo or default_repo()
    channel = channel_id.strip()
    _check_range(from_ts.strip(), to_ts.strip())
    from_ts, to_ts = from_ts.strip(), to_ts.strip()

    with store.transaction() as tx:
        recent = tx.jobs(workspace, RECENT_JOBS)
        open_jobs = [
            job for job in recent
            if str(job["state"]) in OPEN_JOB_STATES
            and (not channel or str(job.get("channel_id") or "") in {"", channel})
        ]
        if open_jobs:
            raise AdminRefused(
                f"아직 끝나지 않은 소급 작업이 있습니다(#{open_jobs[0]['id']})."
                " 끝난 뒤에 다시 거세요."
            )
        if not dry_run:
            counted = [
                job for job in recent
                if bool(job["dry_run"])
                and str(job["state"]) in {"succeeded", "partial"}
                and _same_scope(job, workspace, channel, from_ts, to_ts)
            ]
            if not counted:
                raise AdminRefused(
                    "같은 범위의 미리보기가 먼저 끝나야 합니다. 분량을 세지 않고 걸면"
                    " 중간에 멈춘 것과 다 된 것을 구분할 수 없습니다."
                )
        job = tx.create_job({
            "workspace": workspace, "channel_id": channel,
            "from_ts": from_ts, "to_ts": to_ts, "dry_run": dry_run,
            "requested_by": actor.name, "reason": actor.reason,
        })
        _audit(
            audit_repo, actor, SUBJECT_BACKFILL, workspace, channel,
            field="backfill_dry_run" if dry_run else "backfill_run",
            old="", new=f"{from_ts or '처음'}~{to_ts or '지금'}",
        )
    return {
        "job": job,
        # 만든 것은 요청이다. 「수집됐다」 로 읽히지 않게 화면에 넘긴다.
        "runtimeEffect": (
            "요청을 줄 세웠습니다. supervisor 가 집어가면 상태가 running 으로 바뀝니다."
        ),
    }


def cancel_backfill(
    workspace: str,
    job_id: int,
    actor: Actor,
    repo: SupervisorRepo | None = None,
    *,
    audit_repo=None,
) -> dict:
    """아직 시작하지 않은 요청만 거둔다.

    도는 작업을 콘솔이 취소로 적으면, 실제로는 계속 쓰고 있는데 화면은 멈춘 것으로
    보인다. 그 상태에서 사람은 같은 범위를 한 번 더 건다.
    """
    store = repo or default_repo()
    with store.transaction() as tx:
        job = tx.job(job_id)
        if job is None or str(job["workspace"]) != workspace:
            raise AdminRefused(f"이 워크스페이스의 작업이 아닙니다: #{job_id}")
        if tx.cancel_job(job_id) != 1:
            raise AdminRefused(
                f"이미 시작했거나 끝난 작업이라 취소할 수 없습니다(#{job_id},"
                f" {job['state']})."
            )
        _audit(
            audit_repo, actor, SUBJECT_BACKFILL, workspace,
            str(job.get("channel_id") or ""),
            field="backfill_cancel", old=str(job["state"]), new="cancelled",
        )
    return runtime_detail(workspace, store)


def _audit(repo, actor: Actor, subject: str, workspace: str, channel_id: str,
           *, field: str, old: str, new: str) -> None:
    """감사는 archiving 쪽 표 하나에 모은다. 표를 나누면 화면이 둘을 합쳐 읽어야 한다."""
    store = repo
    if store is None:
        from .archiving_repo import default_repo as archiving_default

        store = archiving_default()
    store.add_audit({
        "actor": actor.name, "subject": subject, "workspace": workspace,
        "channel_id": channel_id, "field": field,
        "old_value": old, "new_value": new, "reason": actor.reason,
    })
