"""VIPBot Socket Mode 런타임 — B-83 조각들이 **처음이자 유일하게** 이어지는 자리.

여기까지 만든 것들은 각자 닫혀 있었다. 권한(`vipbot_runtime_access`), 중복
제거(`vipbot_event_claims`), 답변 엔진(`vipbot_engine`), 전달 정책
(`vipbot_delivery`), 순서(`vipbot_event`). 이 모듈은 그것들을 **잇기만** 한다 —
새 판정도, 새 프롬프트도, 새 Slack 호출도 여기서 만들지 않는다.

## 공용 설정으로 흘러내리지 않는다

VIPBot 은 TYBot 과 **다른 앱 · 다른 DB 계정 · 다른 모델 자격증명**으로 돈다.
그래서 설정이 하나라도 비면 **그 자리에서 멈춘다.** 공용 값으로 메우지 않는다.

- `VIPBOT_WORKSPACE` — 없으면 기동 실패. `WORKSPACE` 같은 공용 이름을 안 본다
- `VIPBOT_ARCHIVE_DIR` — **절대경로**여야 한다. `ARCHIVE_DIR` 을 안 본다
- `VIPBOT_DAILY_LIMIT_USD` — 없으면 기동 실패(`vip_profile`). TYBot 예산을
  나눠 쓰지 않는다
- 모델 키는 systemd 자격증명 하나(`vipbot_anthropic_credential`)
- DB 는 전용 LOGIN 역할 하나(`vipbot_runtime_store.connect_runtime`)

빠진 값을 공용 값으로 메우면 **기동은 성공하고 경계만 사라진다.** 그 상태는
로그에 아무 흔적을 남기지 않으므로, 여기서 시끄럽게 실패하는 쪽을 고른다.

## 신원은 세 곳이 같아야 한다

`auth_test` 가 돌려준 `team_id`·`user_id`, DB 의 `bot_connection` 행, 그리고
들어온 이벤트 봉투의 `team_id`. 셋이 같을 때만 처리한다.

토큰을 잘못 넣으면 봇은 **다른 워크스페이스에 붙어서도 멀쩡히 돈다.** 그때
이 봇은 등록된 근거 채널을 그 조직 사람에게 읽어 주게 된다 — 오류 없이.

## 안 하는 것

수집·백필·첨부 보관·실시간 Slack 조회·files API. 부르지 않을 뿐 아니라
**import 하지 않는다**(`tests/test_vipbot_runtime.py` 가 목록째로 고정한다).
`slack_bolt` 도 함수 안에서 늦게 들여온다 — 유닛의 `ExecStartPre` 가 Slack 없이
`import tybot.vipbot_runtime` 만으로 모듈 건전성을 확인할 수 있어야 한다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .question_files import read_question_files
from .vip_profile import VipProfile, load_profile, question_for_engine
from .vipbot_anthropic_credential import resolve_anthropic_api_key
from .vipbot_engine import (
    ARCHIVE_UNCONFIGURED,
    ARCHIVE_UNREADABLE,
    build_engine,
)
from .vipbot_event import EventResult, handle_event
from .vipbot_event_claims import EventClaims, state_path
from .vipbot_followup import plan_followup
from .vipbot_history import load_vip_history
from .vipbot_history_writer import record_vip_turn
from .vipbot_runtime_access import authorize_runtime
from .vipbot_runtime_store import connect_runtime, load_runtime_config

log = logging.getLogger("tybot.vipbot_runtime")

WORKSPACE_UNCONFIGURED = "vipbot_workspace_unconfigured"
IDENTITY_UNVERIFIED = "vipbot_identity_unverified"
IDENTITY_MISMATCH = "vipbot_identity_mismatch"
CONNECTION_INCOMPLETE = "vipbot_connection_incomplete"

#: 근거가 없다고 답한 것에는 **출처를 붙이지 않는다.** 붙이면 거절이 출처 달린
#: 답변처럼 보이고, 사람은 그 파일이 읽혔다고 읽는다.
NO_SOURCE_REASONS = frozenset(
    {"error", "no_hits", "no_access", "permission_unverified"}
)
ALREADY_RUNNING = "vipbot_already_running"
LOCK_UNAVAILABLE = "vipbot_lock_unavailable"

#: 워크스페이스 키 모양. `vipbot_event`·`vipbot_runtime_access` 와 **같은 규칙**
#: 이어야 한다 — 여기서만 느슨하면 이벤트 쪽에서 조용히 전부 무시된다.
_WORKSPACE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"
_TEAM_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


class VipbotRuntimeError(RuntimeError):
    """기동을 멈춘 이유. ``str()`` 은 고정 낱말 하나다."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeSettings:
    """이 프로세스가 **자기 것으로** 받은 설정. 공용 값은 들어 있지 않다."""

    workspace: str
    archive_dir: Path
    profile: VipProfile


def _workspace(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if (
        not 2 <= len(value) <= 24
        or value[0] not in _WORKSPACE_CHARS[:26]
        or any(character not in _WORKSPACE_CHARS for character in value)
    ):
        return ""
    return value


def _team(raw: object) -> str:
    value = str(raw or "").strip().upper()
    if (
        not 2 <= len(value) <= 24
        or value[0] != "T"
        or any(character not in _TEAM_CHARS for character in value)
    ):
        return ""
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> RuntimeSettings:
    """VIPBot 전용 값만 읽는다. 하나라도 비면 기동하지 않는다."""
    values = os.environ if environ is None else environ

    workspace = _workspace(values.get("VIPBOT_WORKSPACE"))
    if not workspace:
        raise VipbotRuntimeError(WORKSPACE_UNCONFIGURED)

    raw_archive = str(values.get("VIPBOT_ARCHIVE_DIR", "") or "").strip()
    if not raw_archive or not Path(raw_archive).is_absolute():
        # 상대경로는 **작업 디렉터리에 따라 다른 아카이브**를 연다. 서비스가
        # 어디서 기동됐는지에 답변 범위가 달라지는 것은 설정이 아니라 사고다.
        raise VipbotRuntimeError(ARCHIVE_UNCONFIGURED)
    archive_dir = Path(raw_archive)
    if not archive_dir.is_dir():
        raise VipbotRuntimeError(ARCHIVE_UNREADABLE)

    try:
        # 모델·일일 예산·프롬프트 계약. 비용 상한이 없으면 여기서 거절한다 —
        # 코드는 `vip_profile` 의 고정 낱말을 그대로 물려받는다.
        profile = load_profile(values)
    except Exception as exc:  # noqa: BLE001 - 설정 오류는 고정 코드로만 말한다
        raise VipbotRuntimeError(str(getattr(exc, "code", "") or "")) from None

    return RuntimeSettings(
        workspace=workspace, archive_dir=archive_dir, profile=profile
    )


def load_connection(workspace: str) -> dict:
    """전용 DB 계정으로 **자기 워크스페이스의 VIPBot 연결 하나**를 읽는다."""
    config = load_runtime_config(workspace)
    team_id = _team(config.get("team_id"))
    bot_user_id = str(config.get("bot_user_id") or "").strip().upper()
    bot_token = str(config.get("bot_token") or "")
    app_token = str(config.get("app_token") or "")
    if not team_id or not bot_user_id or not bot_token or not app_token:
        # 토큰이 반쪽이면 Socket Mode 는 붙고 답은 못 한다 — 그 상태가 제일
        # 알아채기 어렵다. 기동 전에 끝낸다.
        raise VipbotRuntimeError(CONNECTION_INCOMPLETE)
    return {
        "team_id": team_id,
        "bot_user_id": bot_user_id,
        "bot_token": bot_token,
        "app_token": app_token,
    }


def verify_identity(client, config: Mapping[str, object]) -> dict:
    """``auth_test`` 의 신원이 DB 설정과 같은지 본다. 다르면 기동하지 않는다.

    토큰 한 줄이 어긋나면 봇은 **다른 워크스페이스에 붙어서도 정상으로 보인다.**
    그 상태에서 등록된 근거 채널이 그 조직 사람에게 읽힌다 — 오류 한 줄 없이.
    """
    try:
        identity = client.auth_test() or {}
    except Exception as exc:  # noqa: BLE001 - Slack 오류 본문은 남기지 않는다
        log.warning("VIPBot 신원 확인 실패 err=%s", type(exc).__name__)
        raise VipbotRuntimeError(IDENTITY_UNVERIFIED) from None

    team_id = _team(identity.get("team_id"))
    user_id = str(identity.get("user_id") or "").strip().upper()
    if not team_id or not user_id:
        raise VipbotRuntimeError(IDENTITY_UNVERIFIED)
    if team_id != config.get("team_id") or user_id != config.get("bot_user_id"):
        # 어느 쪽이 틀렸는지는 로그로만 말한다. 값 자체는 운영 식별자라 남기되
        # 토큰은 어디에도 넣지 않는다.
        log.error(
            "VIPBot 신원 불일치 auth_test=%s/%s DB=%s/%s",
            team_id,
            user_id,
            config.get("team_id"),
            config.get("bot_user_id"),
        )
        raise VipbotRuntimeError(IDENTITY_MISMATCH)
    return {"team_id": team_id, "bot_user_id": user_id}


def answer_with_files(engine, question: str, ctx, attached, **kwargs):
    """질문에 붙인 파일을 **이번 답변 한 번의 근거**로 넣는다.

    ## 아카이브에 쓰지 않는다

    VIPBot 은 수집 주체가 아니다(B-83). 이 파일은 저장되지 않고, 다음 질문에서
    다시 열리지도 않는다 — 읽고, 답하고, 버린다. 그래서 「임시」 다.

    ## 출처는 Slack permalink 다

    아카이브에 없는 자료를 아카이브 문서로 인용하면, 사람이 출처를 눌러 갔을 때
    **그 자리에 아무것도 없다**(원칙 2). 붙인 파일의 출처는 그 파일 자신이다.

    ## 검색어는 질문 그대로 둔다

    파일 본문을 질문에 이어 붙이면 그 글자가 검색어가 되어, 바로 옆에 둔 아카이브
    문서를 못 찾는다. `terms` 로 원래 질문을 넘겨 검색 범위를 지킨다.
    """
    lines = tuple(getattr(attached, "lines", ()) or ())
    if not lines:
        return engine.answer(question, ctx, **kwargs)
    block = "\n".join(lines)
    prompt = (
        f"{question}\n\n"
        "<질문에 첨부한 파일>\n"
        f"{block}\n"
        "</질문에 첨부한 파일>"
    )
    answer = engine.answer(prompt, ctx, terms=[question], **kwargs)
    if str(getattr(answer, "reason", "") or "") in NO_SOURCE_REASONS:
        # 근거를 못 찾았다고 답한 것에 출처를 붙이지 않는다.
        return answer
    citations = [
        *(getattr(attached, "citations", ()) or ()),
        *(getattr(answer, "citations", ()) or ()),
    ]
    return replace(answer, citations=list(dict.fromkeys(citations)))


def handle_body(
    body: Mapping[str, object],
    client,
    *,
    settings: RuntimeSettings,
    config: Mapping[str, object],
    claims: EventClaims,
    engine,
    resolver=None,
    history=None,
    history_writer=None,
) -> EventResult:
    """Slack 이벤트 봉투 하나를 검사하고 이벤트 코어에 넘긴다.

    봉투에서 보는 것은 둘뿐이다 — `team_id` 와 `event_id`. 둘 다 **본문이 아니라
    좌표**이고, 하나라도 어긋나면 권한 조회·LLM·Slack 호출 전에 끝난다.
    """
    if not isinstance(body, Mapping):
        return EventResult("ignored", "invalid_event")
    if _team(body.get("team_id")) != config.get("team_id"):
        # 다른 조직의 이벤트다. 한 프로세스가 두 워크스페이스를 섞어 받는 일은
        # 없어야 하고, 섞였다면 그건 토큰이 바뀐 것이다.
        return EventResult("ignored", "invalid_event")
    event = body.get("event")
    if not isinstance(event, Mapping):
        return EventResult("ignored", "invalid_event")

    def authorize(*, workspace: str, slack_user: str):
        # 전용 역할로 연결해 **창구 함수 하나**만 부른다. 사번·권한·근거 표를
        # 직접 읽지 않는다(`vipbot_access_schema.sql`).
        return authorize_runtime(
            connect_runtime, workspace=workspace, slack_user=slack_user
        )

    def generate(question: str, ctx):
        # **사용자 모델 지정은 여기서 사라진다.** 승인된 프로필이 모델을 정하고,
        # `--model=` 은 그 결정을 사용자에게 넘겨주는 길이다.
        cleaned = question_for_engine(question)
        # **권한 판정을 통과한 뒤에만** 파일을 읽는다. `generate` 는
        # `handle_event` 가 `_bound(authorization)` 을 확인한 다음에만 불린다 —
        # 거절당한 사람이 붙인 파일은 내려받지도 않는다.
        attached = read_question_files(
            event.get("files"), str(config.get("bot_token") or "")
        )
        if resolver is None or history is None:
            # 후속 좌표 경로가 안 꽂힌 설치다. 평범한 새 질문처럼 답한다.
            return answer_with_files(engine, cleaned, ctx, attached)
        plan = plan_followup(
            cleaned,
            ctx,
            history=history(
                settings.workspace, str(getattr(ctx, "user_id", "") or "")
            ),
            resolver=resolver,
        )
        if not plan.applied:
            # 후속 지시가 아니다 — **기존 경로 그대로.**
            return answer_with_files(engine, cleaned, ctx, attached)
        # 후속이면 되살린 좌표 **밖으로 나가지 않는다.** 빈 목록도 그대로
        # 넘긴다(`evidence_hits=[]`): 엔진은 그때 검색을 **하지 않는다.**
        # 못 연 좌표를 채널 전체 검색으로 메우면, 사용자는 자기가 물은 것과
        # 다른 자료를 답으로 받고 그 사실을 알 방법이 없다.
        return answer_with_files(
            engine, cleaned, ctx, attached, evidence_hits=list(plan.hits)
        )

    return handle_event(
        event,
        client,
        workspace=settings.workspace,
        bot_user_id=str(config.get("bot_user_id") or ""),
        authorize=authorize,
        generate=generate,
        render=lambda answer: answer.to_slack(),
        event_id=str(body.get("event_id") or ""),
        claim_event=claims.claim,
        record=history_writer,
    )


def build_app(config: Mapping[str, object], *, app_factory=None):
    """Slack 앱 하나. `slack_bolt` 는 **여기서** 늦게 들여온다."""
    if app_factory is None:
        from slack_bolt import App

        app_factory = App
    return app_factory(token=config["bot_token"])


def claims_root() -> Path:
    """VIP 전용 상태 디렉터리. 비용 원장·이벤트 중복 제거와 같은 자리다."""
    return state_path().parent


def build_resolver(archive_dir: Path | str):
    """좌표를 **지금 권한으로** 다시 여는 해석기.

    읽기 전용 창구 하나만 연다. 되살린 좌표는 `ArchiveStore.resolve_refs()` 를
    통과해야 근거가 되고, 그 판정은 요청의 `evidence_sources`(관리형 근거 채널)
    로 이뤄진다 — 지난번에 보였다는 사실은 지금 보여도 된다는 뜻이 아니다.
    """
    from .archive.store import ArchiveStore
    from .thread_followup import ThreadFollowupResolver

    return ThreadFollowupResolver(ArchiveStore(Path(archive_dir)))


def build_history(root: Path):
    """VIP **전용** QA 기록에서 30일 좌표를 읽는다.

    디렉터리가 곧 경계다 — TYBot 의 기록을 읽으면 다른 ACL 로 답한 회차의
    좌표가 VIP 후속 질문에 섞인다. 같은 사람·같은 워크스페이스라도 그 둘은
    **다른 봇의 근거**다.
    """
    qa_root = Path(root) / "qa"

    def load(workspace: str, slack_user: str) -> list[dict]:
        return load_vip_history(qa_root, workspace=workspace, slack_user=slack_user)

    return load


def build_history_writer(root: Path, workspace: str):
    """Build the VIP-only coordinate writer used after successful DM delivery."""
    qa_root = Path(root) / "qa"

    def write(question: str, answer, ctx) -> bool:
        return record_vip_turn(
            qa_root,
            workspace=workspace,
            slack_user=str(getattr(ctx, "user_id", "") or ""),
            question=question_for_engine(question),
            answer=answer,
        )

    return write


def register(
    app,
    *,
    settings: RuntimeSettings,
    config,
    claims,
    engine,
    resolver=None,
    history=None,
    history_writer=None,
) -> None:
    """멘션과 DM 둘만 받는다. 매니페스트가 구독하는 것도 그 둘뿐이다."""

    def on_event(body, client):
        result = handle_body(
            body,
            client,
            settings=settings,
            config=config,
            claims=claims,
            engine=engine,
            resolver=resolver,
            history=history,
            history_writer=history_writer,
        )
        # 수와 고정 낱말만. 질문·답변·근거는 여기 없다.
        log.info(
            "[%s] VIP 이벤트 결과=%s 사유=%s 전달=%s",
            settings.workspace,
            result.outcome,
            result.reason or "-",
            getattr(result.delivery, "outcome", "-"),
        )
        return result

    app.event("app_mention")(on_event)
    app.event("message")(on_event)


def _lock_file(workspace: str, directory: Path | None = None) -> Path:
    """잠금 파일 자리. `STATE_DIRECTORY` 는 `state_path` 가 이미 검증한 값이다."""
    root = state_path().parent if directory is None else Path(directory)
    return root / f"vipbot-{workspace}.lock"


def _take(handle) -> bool:
    """이미 누가 쥐고 있으면 거짓. **기다리지 않는다.**"""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:  # 닫는 길은 막지 않는다
        log.warning("VIPBot 잠금 해제 실패 err=%s", type(exc).__name__)


@contextmanager
def single_instance(workspace: str, *, directory: Path | None = None):
    """한 호스트에서 **같은 워크스페이스 VIPBot 하나**만 돌게 한다.

    봇이 둘이면 같은 멘션에 두 번 답하고 모델 비용도 두 배다. systemd 는 같은
    유닛의 중복 기동만 막는다 — 손으로 띄운 프로세스나 복사해 만든 두 번째
    유닛은 막지 못한다. 그 자리를 파일 잠금이 메운다.

    **호스트 단위다.** 여러 서버가 같은 `/var/lib` 를 공유하지 않는 한 그걸로
    충분하고, 공유한다면 잠금이 아니라 배포가 틀린 것이다.
    """
    path = _lock_file(workspace, directory)
    try:
        handle = path.open("a+", encoding="utf-8")
    except OSError:
        raise VipbotRuntimeError(LOCK_UNAVAILABLE) from None
    if not _take(handle):
        handle.close()
        # 두 번째 프로세스는 **아무것도 안 하고** 끝난다. DB 도 Slack 도
        # 건드리지 않았으므로 되돌릴 것이 없다.
        raise VipbotRuntimeError(ALREADY_RUNNING)
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError as exc:  # PID 는 사람을 위한 힌트일 뿐이다
        log.warning("VIPBot 잠금 PID 기록 실패 err=%s", type(exc).__name__)
    try:
        yield path
    finally:
        _release(handle)
        handle.close()


def _socket_handler(app, app_token: str):
    """Socket Mode 손잡이. `slack_bolt` 는 **여기서** 늦게 들여온다."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    return SocketModeHandler(app, app_token)


def _close(handler) -> None:
    """손잡이를 닫는다. **닫다가 난 오류가 원래 오류를 가리지 않게** 삼킨다."""
    closer = getattr(handler, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception as exc:  # noqa: BLE001 - 종료 경로를 막지 않는다
        log.warning("VIPBot 소켓 종료 실패 err=%s", type(exc).__name__)


def main(argv: list[str] | None = None) -> int:
    """기동. **설정이 하나라도 비면 Slack 에 붙기 전에 멈춘다.**

    순서에 이유가 있다. 설정 → 잠금 → 자격증명 → DB → 엔진 → 앱 → 신원 →
    손잡이. 잠금을 앞에 두는 이유는 **두 번째 프로세스가 DB 와 Slack 을 건드리기
    전에** 끝나야 하기 때문이고, 신원을 손잡이보다 앞에 두는 이유는 틀린 토큰으로
    남의 워크스페이스에 **붙고 나서** 알아차리면 늦기 때문이다.
    """
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    with single_instance(settings.workspace):
        api_key = resolve_anthropic_api_key()
        config = load_connection(settings.workspace)
        claims = EventClaims(state_path())
        engine = build_engine(
            archive_dir=settings.archive_dir,
            profile=settings.profile,
            anthropic_api_key=api_key,
        )
        app = build_app(config)
        verify_identity(app.client, config)
        register(
            app,
            settings=settings,
            config=config,
            claims=claims,
            engine=engine,
            resolver=build_resolver(settings.archive_dir),
            history=build_history(claims_root()),
            history_writer=build_history_writer(claims_root(), settings.workspace),
        )

        log.info(
            "VIPBot 기동 ws=%s team=%s bot=%s model=%s",
            settings.workspace,
            config["team_id"],
            config["bot_user_id"],
            settings.profile.model,
        )
        handler = _socket_handler(app, config["app_token"])
        try:
            handler.start()
        finally:
            _close(handler)
    return 0


__all__ = [
    "ALREADY_RUNNING",
    "CONNECTION_INCOMPLETE",
    "IDENTITY_MISMATCH",
    "IDENTITY_UNVERIFIED",
    "LOCK_UNAVAILABLE",
    "WORKSPACE_UNCONFIGURED",
    "RuntimeSettings",
    "VipbotRuntimeError",
    "build_app",
    "handle_body",
    "load_connection",
    "load_settings",
    "main",
    "register",
    "single_instance",
    "verify_identity",
]


if __name__ == "__main__":  # pragma: no cover - 기동 경로
    raise SystemExit(main())
