"""프로세스 **하나**가 여러 워크스페이스를 맡는다.

설계: `docs/design/archiver-supervisor-backfill-console-2026-09-29.md` §1~§3
상태 규칙: `supervisor_state` · Socket 묶음: `token_topology`

## 무엇이 문제였나

workspace 마다 systemd 인스턴스를 만들면 워크스페이스를 추가할 때마다 서버에
들어가 env 파일과 unit 을 만들어야 한다. **쓸 수 있는 사람이 한 명**이라는 뜻이고,
그 사람이 없으면 기능이 없는 것과 같다(CLAUDE.md).

그리고 더 나쁜 것이 있다. 같은 Slack 앱을 두 워크스페이스에 설치한 경우,
workspace 마다 Socket 을 열면 **Slack 이 이벤트를 둘 중 아무 쪽에나 보낸다.**
한쪽 worker 만 받고 다른 쪽은 조용하다 — 오류가 아니라 누락이다.

## 이 모듈이 정하는 것

| 무엇 | 규칙 |
|---|---|
| Socket 수 | App Token 하나당 하나(`token_topology`) |
| 이벤트 배분 | `team_id` 로 워크스페이스를 찾는다 |
| 모르는 team | **fail closed** — ACK 만 하고 저장하지 않는다 |
| 한 워크스페이스 장애 | 그 워크스페이스만 `error`. 나머지는 계속 돈다 |
| 같은 Bot Token 중복 | **기동 거부** — 남의 워크스페이스에 수집한다 |

## 여기서 하지 않는 것

Socket 을 실제로 열지 않는다. 이 모듈은 **누가 무엇을 맡는지**를 정하고, 연결과
스레드는 부르는 쪽(서비스 진입점)이 만든다. 그래야 시험이 Slack 없이 규칙을 본다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .supervisor_state import (
    DesiredMode,
    ObservedState,
    WorkspaceRuntime,
    plan_observed_change,
)
from .token_topology import Connection, fingerprint

log = logging.getLogger("tybot.archive.supervisor")


class StartupRefused(RuntimeError):
    """이 구성으로는 뜨지 않는다. **사유를 사람 말로 들고 있다.**"""


@dataclass(frozen=True)
class Binding:
    """한 워크스페이스가 어느 Socket 에 붙는가."""

    workspace: str
    team_id: str
    bot_token: str
    socket_key: str

    def __repr__(self) -> str:  # pragma: no cover - 로그에 토큰이 찍히지 않게
        return f"Binding(workspace={self.workspace!r}, team_id={self.team_id!r})"


@dataclass
class SocketWorker:
    """App Token 하나가 맡는 워크스페이스들."""

    socket_key: str
    app_token: str
    bindings: dict[str, Binding] = field(default_factory=dict)

    def workspace_for(self, team_id: str) -> Binding | None:
        return self.bindings.get(team_id)

    def __repr__(self) -> str:  # pragma: no cover
        return f"SocketWorker(workspaces={sorted(b.workspace for b in self.bindings.values())})"


def plan_workers(connections: list[Connection]) -> list[SocketWorker]:
    """연결 목록에서 **열어야 할 Socket** 을 만든다.

    같은 App Token 은 한 worker 로 묶고, 다른 App Token 은 같은 supervisor 안에서
    별도 worker 가 된다. 프로세스를 나누지 않는 이유는 워크스페이스 추가가 곧 서버
    작업이 되지 않게 하기 위해서다.

    기동을 거부하는 두 경우가 있다.

    - **같은 Bot Token 이 여러 워크스페이스에 붙어 있다.** 한쪽 토큰을 다른 연결에
      붙여 넣은 것이고, 그대로 뜨면 남의 워크스페이스에 수집한다. 오류가 아니라
      유출이라 뜨기 전에 막는다
    - **한 Socket 안에서 team_id 가 겹친다.** 이벤트를 어느 워크스페이스로 보낼지
      정할 수 없다. 추측해서 보내면 그 추측이 조용히 틀린다
    """
    by_bot: dict[str, list[str]] = {}
    workers: dict[str, SocketWorker] = {}

    for item in connections:
        if not item.app_token or not item.bot_token:
            raise StartupRefused(
                f"{item.workspace} 연결에 봇/앱 토큰 쌍이 없습니다. Socket 을 열 수 없습니다."
            )
        if not item.team_id:
            raise StartupRefused(
                f"{item.workspace} 연결의 Slack team 이 확인되지 않았습니다."
                " 신원 확인을 먼저 하세요."
            )
        by_bot.setdefault(fingerprint(item.bot_token), []).append(item.workspace)

        socket_key = fingerprint(item.app_token)
        worker = workers.setdefault(
            socket_key, SocketWorker(socket_key=socket_key, app_token=item.app_token),
        )
        if item.team_id in worker.bindings:
            other = worker.bindings[item.team_id].workspace
            raise StartupRefused(
                f"같은 Slack 앱에 team {item.team_id} 가 두 번 연결돼 있습니다"
                f"({other}, {item.workspace}). 이벤트를 어디로 보낼지 정할 수 없습니다."
            )
        worker.bindings[item.team_id] = Binding(
            workspace=item.workspace, team_id=item.team_id,
            bot_token=item.bot_token, socket_key=socket_key,
        )

    shared = sorted(
        sorted(workspaces) for workspaces in by_bot.values() if len(workspaces) > 1
    )
    if shared:
        raise StartupRefused(
            "같은 Bot Token 이 여러 워크스페이스에 연결돼 있습니다: "
            + " · ".join(", ".join(group) for group in shared)
            + ". 한쪽 토큰이 다른 연결에 들어가 있으면 남의 대화를 수집합니다."
        )
    return sorted(workers.values(), key=lambda worker: worker.socket_key)


@dataclass
class Supervisor:
    """워크스페이스 worker 들의 **주인.** 한 프로세스 안에 산다."""

    workers: list[SocketWorker]
    runtimes: dict[str, WorkspaceRuntime] = field(default_factory=dict)
    #: 그 워크스페이스가 마지막으로 읽은 설정 세대. 오래된 worker 는 설정을 적용하지
    #: 않는다 — 콘솔이 바꾼 값을 옛 worker 가 덮으면 끈 것이 되살아난다.
    applied_generation: dict[str, int] = field(default_factory=dict)

    def bindings(self) -> dict[str, Binding]:
        return {
            binding.workspace: binding
            for worker in self.workers
            for binding in worker.bindings.values()
        }

    def route(self, socket_key: str, team_id: str) -> Binding | None:
        """이 이벤트는 누구 것인가. **모르면 `None`.**

        추측하지 않는다. 모르는 team 에서 온 이벤트를 아무 워크스페이스로 보내면
        그 대화가 남의 아카이브에 들어가고, 그건 되돌릴 수 없다(절대 원칙 3·4).
        """
        worker = next((w for w in self.workers if w.socket_key == socket_key), None)
        if worker is None:
            return None
        return worker.workspace_for(team_id)

    def observe(
        self, workspace: str, state: ObservedState | str, *, error_code: str = "",
    ) -> WorkspaceRuntime:
        """worker 가 자기 상태를 보고한다. **한 워크스페이스의 값만 바뀐다.**"""
        current = self.runtimes.get(workspace) or WorkspaceRuntime(workspace=workspace)
        updated = plan_observed_change(current, state, error_code=error_code)
        self.runtimes[workspace] = updated
        return updated

    def should_apply(self, workspace: str, generation: int) -> bool:
        """이 세대 설정을 적용해도 되나. **오래된 것은 안 된다.**

        느린 worker 가 뒤늦게 옛 설정을 적용하면, 콘솔에서 끈 워크스페이스가 다시
        돌기 시작한다. 그 되살아남은 화면 어디에도 안 보인다.
        """
        applied = self.applied_generation.get(workspace, 0)
        if generation < applied:
            log.info(
                "오래된 설정 세대라 적용하지 않는다 ws=%s gen=%s applied=%s",
                workspace, generation, applied,
            )
            return False
        self.applied_generation[workspace] = generation
        return True

    def isolate(self, workspace: str, error_code: str) -> WorkspaceRuntime:
        """한 워크스페이스를 멈춘다. **나머지는 그대로 돈다.**

        한 워크스페이스의 장애로 프로세스를 내리면 다른 본부 수집까지 멈춘다.
        그쪽 사람들은 자기 일과 무관한 이유로 자료를 잃는다.
        """
        return self.observe(workspace, ObservedState.ERROR, error_code=error_code)

    def dispatch(
        self,
        socket_key: str,
        event: dict,
        *,
        handler: Callable[[Binding, dict], str],
        unknown_team: Callable[[str], None] | None = None,
    ) -> str:
        """이벤트 하나를 맡은 워크스페이스로 보낸다.

        돌려주는 값은 `handler` 의 결과이거나 건너뛴 사유다. 예외는 그 워크스페이스
        안에서 끝난다 — 한 건의 실패가 Socket 을 끊으면 같은 Socket 을 쓰는 다른
        워크스페이스까지 조용해진다.
        """
        team_id = str(event.get("team_id") or event.get("team") or "")
        binding = self.route(socket_key, team_id) if team_id else None
        if binding is None:
            # **fail closed.** ACK 는 Slack 쪽이 하고, 우리는 저장하지 않는다.
            # 본문·토큰을 남기지 않고 좌표만 적는다.
            log.warning("모르는 team 이벤트라 저장하지 않는다 team=%s", team_id or "(없음)")
            if unknown_team is not None:
                unknown_team(team_id)
            return "skipped-unknown-team"

        runtime = self.runtimes.get(binding.workspace)
        if runtime is not None and runtime.desired_mode is DesiredMode.OFF:
            # 사람이 끈 워크스페이스다. worker 가 아직 멈추는 중일 수 있으므로
            # 상태를 오류로 만들지 않고 **받은 것만 버린다.**
            return "skipped-desired-off"

        try:
            return handler(binding, event)
        except Exception as exc:
            log.exception("[%s] 이벤트 처리 실패", binding.workspace)
            self.isolate(binding.workspace, error_code=type(exc).__name__)
            return "failed"
