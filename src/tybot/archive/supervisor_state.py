"""Archiver supervisor 의 **상태 규칙.** DB 도 Slack 도 모른다.

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §3.2·§4.2
스키마: `deploy/sql/archiver_supervisor_schema.sql`

## 왜 순수 함수인가

`archiving_state` 와 같은 이유다. 상태 판단이 SQL·Slack 호출과 섞이면 시험이
그 둘을 흉내 내야 하고, 흉내는 진짜와 달라진다. 여기서는 **값만 받아 값만
돌려준다** — 시험이 규칙 자체를 본다.

## 희망과 관측을 나눈다

사람이 원하는 것(`desired`)과 프로세스가 하는 것(`observed`)은 다를 수 있고,
그 차이가 곧 「무엇이 고장났나」 다. 한 칸에 합치면 「켜라고 했는데 안 켜졌다」 를
표현할 자리가 없어진다.

## 파일럿에서 `live` 는 없다

운영 원문의 주인을 바꾸는 일이라 release gate·채널별 writer 인수·Master writer
중지를 전부 지나야 한다. 화면에서 고를 수 있게 두면 언젠가 눌린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DesiredMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    #: 파일럿에서는 **코드가 거절한다**(`plan_desired_change`).
    LIVE = "live"


class ObservedState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    #: 돌지만 온전치 않다. `error` 와 합치면 「멈췄다」 와 「절반만 된다」 를
    #: 구분할 수 없고, 사람이 할 일이 달라진다.
    DEGRADED = "degraded"
    ERROR = "error"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: 일부만 됐다. **0건 성공과 다르다** — 남은 구간이 있다는 뜻이다.
    PARTIAL = "partial"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 파일럿에서 고를 수 있는 희망 상태.
PILOT_MODES = frozenset({DesiredMode.OFF, DesiredMode.SHADOW})

#: 끝난 작업. 여기서 다시 나가지 않는다.
TERMINAL_JOB_STATES = frozenset({
    JobState.PARTIAL, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
})


class TransitionRefused(ValueError):
    """이 전이는 안 된다. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class WorkspaceRuntime:
    """한 워크스페이스의 지금 상태."""

    workspace: str
    desired_mode: DesiredMode = DesiredMode.OFF
    observed_state: ObservedState = ObservedState.STOPPED
    generation: int = 1
    error_code: str = ""

    @property
    def collecting(self) -> bool:
        """지금 실제로 모으고 있나. **희망이 아니라 관측으로 답한다.**"""
        return self.observed_state in (ObservedState.RUNNING, ObservedState.DEGRADED)


def plan_desired_change(
    current: WorkspaceRuntime, target: DesiredMode | str, *, allow_live: bool = False,
) -> WorkspaceRuntime:
    """사람이 희망 상태를 바꾼다. **세대를 올린다.**

    세대가 안 오르면 supervisor 가 「바뀐 것이 없다」 고 보고 계속 옛 설정으로 돈다.
    그 상태는 조용하다 — 콘솔은 바뀐 값을 보여 주고 프로세스는 옛 값으로 돈다.

    `live` 는 파일럿에서 거절한다. 여는 것은 별도 결정이고, 그 결정을 이 함수의
    기본값으로 만들지 않는다.
    """
    wanted = DesiredMode(target)
    if wanted is DesiredMode.LIVE and not allow_live:
        raise TransitionRefused(
            "운영 수집(live)은 파일럿 범위 밖입니다. release gate·채널별 writer"
            " 인수·Master writer 중지를 먼저 지나야 합니다."
        )
    if wanted is current.desired_mode:
        # 같은 값을 다시 눌렀다. 세대를 올리면 worker 가 공연히 재시작한다.
        return current
    return WorkspaceRuntime(
        workspace=current.workspace,
        desired_mode=wanted,
        observed_state=current.observed_state,
        generation=current.generation + 1,
        error_code=current.error_code,
    )


def plan_observed_change(
    current: WorkspaceRuntime, observed: ObservedState | str, *, error_code: str = "",
) -> WorkspaceRuntime:
    """supervisor 가 관측 상태를 보고한다.

    `off` 를 원하는데 `running` 을 보고하면 거절한다 — 둘 중 하나가 거짓이고,
    그 상태를 저장하면 화면이 「꺼져 있는데 수집 중」 을 보여 준다.

    오류 코드는 **오류일 때만** 남는다. 정상인데 코드가 남아 있으면 화면이
    「정상인데 빨간 글씨」 가 되고, 그걸 본 사람은 무엇을 믿어야 할지 모른다.
    """
    state = ObservedState(observed)
    if current.desired_mode is DesiredMode.OFF and state in (
        ObservedState.STARTING, ObservedState.RUNNING, ObservedState.DEGRADED,
    ):
        raise TransitionRefused(
            f"{current.workspace} 는 수집을 끄기로 돼 있는데 {state} 로 보고됐습니다."
        )
    if state not in (ObservedState.ERROR, ObservedState.DEGRADED) and error_code:
        raise TransitionRefused("오류가 아닌 상태에 오류 코드를 남길 수 없습니다.")
    if state in (ObservedState.ERROR, ObservedState.DEGRADED) and not error_code:
        raise TransitionRefused("오류 상태에는 사유 코드가 필요합니다.")
    return WorkspaceRuntime(
        workspace=current.workspace,
        desired_mode=current.desired_mode,
        observed_state=state,
        generation=current.generation,
        error_code=error_code,
    )


def stale(heartbeat_age_seconds: float | None, *, limit_seconds: float = 180) -> bool:
    """heartbeat 가 끊겼나. **한 번도 없었던 것도 끊긴 것이다.**

    `None` 을 「아직 판단 못 함」 으로 두면, 한 번도 안 뜬 worker 가 영원히 정상으로
    보인다.
    """
    if heartbeat_age_seconds is None:
        return True
    return heartbeat_age_seconds > limit_seconds


# ---------------------------------------------------------------------------
# 소급 수집 작업
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JobCounts:
    """무엇을 몇 건 했나. **발견과 기록을 나눠 센다.**

    같은 수가 아니고, 다르면 그 차이가 곧 중복·거부·실패다. 하나로 합치면
    「500건 처리」 가 실제로는 400건 중복이었다는 사실을 가린다.
    """

    found: int = 0
    written: int = 0
    duplicate: int = 0
    refused: int = 0
    failed: int = 0

    @property
    def accounted(self) -> int:
        return self.written + self.duplicate + self.refused + self.failed


def finish_state(counts: JobCounts, *, exhausted: bool) -> JobState:
    """작업을 어떤 상태로 닫나.

    **0건과 실패를 같은 결과로 만들지 않는다**(§4.2). 아무것도 없었던 것은 성공이고,
    못 읽은 것은 실패다 — 사람이 할 일이 다르다.

    `exhausted` 는 범위를 끝까지 읽었나다. 중간에 끊겼으면 건수가 맞아도 `partial`
    이다. 남은 구간이 있다는 사실이 건수보다 중요하다.
    """
    if counts.failed and not counts.written:
        return JobState.FAILED
    if counts.failed or not exhausted:
        return JobState.PARTIAL
    return JobState.SUCCEEDED


def plan_job_state(current: JobState | str, target: JobState | str) -> JobState:
    """작업 상태 전이. 끝난 작업은 **다시 안 돈다.**

    되돌릴 수 있게 두면 같은 작업이 두 번 돌고, 소급은 되돌릴 수 없는 종류의 일이다.
    """
    now, wanted = JobState(current), JobState(target)
    if now in TERMINAL_JOB_STATES:
        raise TransitionRefused(f"이미 끝난 작업입니다: {now}")
    if now is JobState.QUEUED and wanted not in (JobState.RUNNING, JobState.CANCELLED):
        raise TransitionRefused(f"대기 중인 작업은 {wanted} 로 갈 수 없습니다.")
    if now is JobState.RUNNING and wanted is JobState.QUEUED:
        raise TransitionRefused("도는 작업을 대기로 되돌릴 수 없습니다.")
    return wanted


def advance_cursor(current: str, candidate: str, *, durable: bool) -> str:
    """cursor 를 옮긴다. **쓰기가 끝난 뒤에만.**

    읽자마자 옮기면 그 사이에 실패한 구간이 영영 안 메워진다. 그 구멍은 조용하다 —
    다음 실행은 이미 지난 곳부터 읽기 때문이다.

    뒤로는 안 간다. 재실행이 겹치는 구간을 다시 읽어도 cursor 는 제자리다.
    """
    if not durable:
        return current
    if not candidate:
        return current
    if not current:
        return candidate
    # Slack ts 는 `1727600000.000100` 모양이라 문자열 비교로 순서가 맞다.
    return max(current, candidate)
